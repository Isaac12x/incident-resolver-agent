"""Command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import sys
import time
import urllib.request
from pathlib import Path

import uvicorn

from .app import Application
from .bundles import (
    activate_bundle,
    build_bundle,
    list_bundles,
    load_active_bundle,
    rollback_bundle,
)
from .cli_help import DESCRIPTION, HELP, AgentParser, HelpFormatter, format_cli_help
from .config import ConnectorConfig, load_config, save_config
from .dashboard.cli import add_dashboard_parser, run_dashboard_command
from .executions import run_executions_command
from .lifecycle import (
    bootstrap,
    default_config_path,
    default_runtime_path,
    doctor,
    ensure_runtime_tools,
    ensure_user_config,
    require_ready,
    update_installation,
)
from .models import Incident, TaskState
from .plugins import connect_source, connector_from_plugin, plugin_catalog
from .server import create_server
from .systemd_env import export_systemd_environment, local_service_base_url, service_base_url
from .tooling import (
    build_repository_graphs,
    capture_structured_tree,
    initialise_runtime_tree,
    install_configured_repositories,
)
from .tui import run_tui


def build_parser() -> argparse.ArgumentParser:
    parser = AgentParser(
        prog="incident-agent",
        description=DESCRIPTION,
        epilog=format_cli_help(),
        formatter_class=HelpFormatter,
    )
    parser.add_argument("--config", type=Path, default=None, metavar="PATH")
    commands = parser.add_subparsers(
        dest="command", metavar="COMMAND", required=False, parser_class=AgentParser
    )
    plugins = commands.add_parser("plugins", help=HELP["plugins"])
    plugins.add_argument("action", nargs="?", choices=("list",), default="list")
    plugins.add_argument("--json", action="store_true", dest="as_json")
    connect = commands.add_parser(
        "connect",
        help=HELP["connect"],
        description=(
            "Add a named source adapter. Omit PLUGIN to set one up interactively. "
            "Use --list to print configured sources without endpoints or secrets."
        ),
        formatter_class=HelpFormatter,
    )
    connect.add_argument("plugin", nargs="?", help="built-in adapter name")
    connect.add_argument("--list", action="store_true", dest="list_sources")
    connect.add_argument("--name")
    connect.add_argument("--url")
    connect.add_argument("--log-path")
    connect.add_argument("--transport", choices=("stdio", "streamable-http", "sse"))
    connect.add_argument("--auth-token-env")
    connect.add_argument("--tenant-id")
    connect.add_argument("--datasource-uid")
    connect.add_argument("--purpose", choices=("incident", "output", "observability", "other"))
    connect.add_argument("--capability", action="append", default=[])
    connect.add_argument(
        "--command",
        dest="source_command",
        nargs=argparse.REMAINDER,
        help="stdio executable and arguments (must be last)",
    )
    commands.add_parser("init", help=HELP["init"])
    serve = commands.add_parser("serve", help=HELP["serve"])
    serve.add_argument("--no-worker", action="store_true")
    commands.add_parser("worker", help=HELP["worker"])
    commands.add_parser("tui", aliases=["config"], help=HELP["config"])
    commands.add_parser("update", help=HELP["update"])
    doctor_command = commands.add_parser("doctor", help=HELP["doctor"])
    doctor_command.add_argument(
        "--install", action="store_true", help="install missing helper CLIs with uv"
    )
    commands.add_parser("status", help=HELP["status"])
    bundle = commands.add_parser("bundle", help=HELP["bundle"])
    bundle.add_argument("action", choices=("build", "list", "activate", "rollback"))
    bundle.add_argument("version", nargs="?")
    commands.add_parser("mcp", help=HELP["mcp"])
    install_repositories = commands.add_parser(
        "install-repositories",
        help=HELP["install-repositories"],
    )
    install_repositories.add_argument("--source-root", type=Path, required=True)
    install_repositories.add_argument("--destination-root", type=Path, required=True)
    export_env = commands.add_parser(
        "export-systemd-env",
        help=HELP["export-systemd-env"],
    )
    export_env.add_argument(
        "--output",
        type=Path,
        default=Path("/run/incident-harness/environment"),
    )
    export_env.add_argument(
        "--secrets",
        type=Path,
        action="append",
        help="secret store to read (default: /etc/incident-harness/environment and .env)",
    )
    commands.add_parser("service-url", help=HELP["service-url"])
    healthcheck = commands.add_parser("healthcheck", help=HELP["healthcheck"])
    healthcheck.add_argument("--timeout", type=float, default=30.0)
    run = commands.add_parser(
        "run",
        help=HELP["run"],
        description=(
            "Submit an incident JSON file and print the resulting task. "
            "Omit FILE to start the HTTP server and worker in the foreground."
        ),
        formatter_class=HelpFormatter,
    )
    run.add_argument("incident", type=Path, nargs="?", metavar="FILE")
    eval_command = commands.add_parser("eval", help=HELP["eval"])
    eval_command.add_argument(
        "--suite",
        choices=("contracts", "repair", "root-cause", "retrieval"),
        default="contracts",
    )
    eval_command.add_argument("dataset", type=Path, nargs="?")
    eval_command.add_argument("--output", type=Path)
    index = commands.add_parser("index", help=HELP["index"])
    index.add_argument("path", type=Path, nargs="?", default=Path("."))
    tree = commands.add_parser("tree", help=HELP["tree"])
    tree.add_argument("path", type=Path, nargs="?", default=Path("."))
    tree.add_argument("--out", type=Path, default=Path("structure.seed"))
    add_dashboard_parser(commands)
    executions = commands.add_parser(
        "executions",
        help=HELP["executions"],
        description=(
            "List previous task sessions, or inspect the stored model conversation. "
            "SESSION_ID is the agent's durable id: task:<task-id>."
        ),
        formatter_class=HelpFormatter,
        epilog=(
            "examples:\n"
            "  incident-agent executions\n"
            "  incident-agent executions list\n"
            "  incident-agent executions task:ID inspect\n"
        ),
    )
    executions.add_argument(
        "target",
        nargs="?",
        default="list",
        metavar="SESSION_ID",
        help="list, or a task session id (task:<task-id>)",
    )
    executions.add_argument(
        "action",
        nargs="?",
        choices=("inspect",),
        help="open or print the stored model conversation",
    )
    return parser


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        raise SystemExit(0)
    return args


async def _worker(application: Application) -> None:
    await application.connectors.start()
    try:
        await application.workflow.run_worker()
    finally:
        await application.connectors.stop()


async def _run_direct(application: Application, path: Path) -> None:
    incident = Incident.model_validate_json(path.read_text(encoding="utf-8"))
    await application.connectors.start()
    try:
        task = await application.workflow.submit(incident)
        while task.state not in {
            TaskState.WAITING_FOR_DEPLOYMENT,
            TaskState.WAITING_FOR_REVIEW,
            TaskState.COMPLETED,
            TaskState.BLOCKED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }:
            task = await application.workflow.process(task.task_id)
        print(json.dumps(task.model_dump(mode="json"), indent=2))
    finally:
        await application.connectors.stop()


def _load_eval_records(path: Path) -> list[dict[str, object]]:
    """Read and validate a JSON array or JSONL list of evaluation records."""
    try:
        raw = path.read_text(encoding="utf-8")
        decoded = json.loads(raw)
        values = decoded if isinstance(decoded, list) else [decoded]
    except json.JSONDecodeError:
        try:
            values = [json.loads(line) for line in raw.splitlines() if line.strip()]
        except json.JSONDecodeError as error:
            raise SystemExit(f"invalid evaluation JSON/JSONL dataset: {error}") from error
    except (OSError, UnicodeDecodeError) as error:
        raise SystemExit(f"could not read evaluation dataset: {error}") from error
    if not values or any(not isinstance(item, dict) for item in values):
        raise SystemExit("evaluation dataset must contain JSON objects")
    return [item for item in values if isinstance(item, dict)]


def _print_plugins(*, as_json: bool = False) -> None:
    catalog = plugin_catalog()
    if as_json:
        print(json.dumps(catalog, indent=2))
        return
    for item in catalog:
        capabilities = ", ".join(item.get("capabilities", []))
        suffix = f" [{capabilities}]" if capabilities else ""
        print(f"{item['name']}: {item['description']}{suffix}")


def _interactive_connect(catalog: list[dict[str, object]]) -> tuple[str, str, dict[str, object]]:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise SystemExit("connect requires PLUGIN and --name NAME in a non-interactive terminal")
    if not catalog:
        raise SystemExit("no source adapters are available")
    print("Available source adapters:")
    for index, item in enumerate(catalog, 1):
        print(f"{index}. {item['name']} - {item['description']}")
    selection = input("Adapter number: ").strip()
    try:
        selected = int(selection)
        if selected < 1:
            raise ValueError
        item = catalog[selected - 1]
    except (ValueError, IndexError) as error:
        raise SystemExit("invalid adapter selection") from error
    plugin = str(item["name"])
    name = input(f"Source name [{plugin}]: ").strip() or plugin
    options: dict[str, object] = {}
    if plugin == "mcp":
        transport = input("Transport (streamable-http/sse/stdio) [streamable-http]: ").strip()
        options["transport"] = transport or "streamable-http"
        if options["transport"] == "stdio":
            options["command"] = shlex.split(input("Command: "))
        else:
            options["url"] = input("URL: ").strip()
    elif plugin == "local-logs":
        options["log_path"] = input("Absolute log path: ").strip()
    elif plugin in {"loki", "grafana"}:
        options["url"] = input("URL: ").strip()
        if plugin == "grafana":
            options["datasource_uid"] = input("Loki datasource UID: ").strip()
    if plugin in {"loki", "grafana"} or (
        plugin == "mcp" and options.get("transport") != "stdio"
    ):
        auth_env = input("Auth token environment variable (optional): ").strip()
        if auth_env:
            options["auth_token_env"] = auth_env
    capability_prompt = (
        "Capabilities (comma separated, required): "
        if plugin == "mcp"
        else "Capabilities (comma separated, optional): "
    )
    capabilities = input(capability_prompt).strip()
    if capabilities:
        options["capabilities"] = [
            value.strip() for value in capabilities.split(",") if value.strip()
        ]
    if plugin == "mcp" and not options.get("capabilities"):
        raise SystemExit("MCP connectors require at least one capability")
    return plugin, name, options


def _configuration_error(error: Exception) -> str:
    """Render validation locations and messages without including supplied values."""
    details = getattr(error, "errors", None)
    if callable(details):
        messages = []
        for item in details():
            location = ".".join(str(part) for part in item.get("loc", ())) or "connector"
            messages.append(f"{location}: {item.get('msg', 'invalid value')}")
        if messages:
            return "; ".join(messages)
    return str(error).splitlines()[0]


def _connect_command(args: argparse.Namespace) -> None:
    if args.list_sources:
        if (
            args.plugin
            or args.name
            or any(
                getattr(args, field) is not None
                for field in (
                    "url",
                    "log_path",
                    "transport",
                    "auth_token_env",
                    "tenant_id",
                    "datasource_uid",
                    "purpose",
                    "source_command",
                )
            )
            or args.capability
        ):
            raise SystemExit("connect --list cannot be combined with connection options")
        config_path = args.config or default_config_path()
        try:
            config = load_config(config_path, create=False)
        except (OSError, ValueError) as error:
            raise SystemExit(
                f"could not read configuration: {_configuration_error(error)}"
            ) from error
        for connector in config.connectors:
            capabilities = ",".join(connector.capabilities)
            print(f"{connector.name}\t{connector.type}\t{capabilities}".rstrip("\t"))
        return

    catalog = plugin_catalog()
    known = {str(item["name"]) for item in catalog}
    if args.plugin is None:
        supplied = any(
            getattr(args, field) is not None
            for field in (
                "name",
                "url",
                "log_path",
                "transport",
                "auth_token_env",
                "tenant_id",
                "datasource_uid",
                "purpose",
                "source_command",
            )
        ) or bool(args.capability)
        if supplied:
            raise SystemExit("connect requires PLUGIN before connection options")
        try:
            plugin, name, options = _interactive_connect(catalog)
        except (EOFError, KeyboardInterrupt) as error:
            raise SystemExit("connect cancelled") from error
        except ValueError as error:
            raise SystemExit(f"invalid stdio command: {error}") from error
    else:
        plugin = args.plugin
        if plugin not in known:
            raise SystemExit(f"unknown source adapter: {plugin}")
        if not args.name:
            raise SystemExit("connect requires --name NAME in a non-interactive terminal")
        name = args.name
        options = {
            key: value
            for key, value in {
                "url": args.url,
                "log_path": args.log_path,
                "transport": args.transport,
                "auth_token_env": args.auth_token_env,
                "tenant_id": args.tenant_id,
                "datasource_uid": args.datasource_uid,
                "purpose": args.purpose,
                "capabilities": args.capability,
                "command": args.source_command,
            }.items()
            if value is not None and value != []
        }
    try:
        connector = connector_from_plugin(plugin, name, **options)
        config_path = args.config or default_config_path()
        config = load_config(config_path, create=False)
        if not config_path.exists() and config_path == default_config_path():
            config.runtime_root = default_runtime_path()
            config.server.require_api_auth = True
        connect_source(config, connector)
        save_config(config, config_path)
    except (ValueError, TypeError, OSError) as error:
        raise SystemExit(f"could not configure source: {_configuration_error(error)}") from error
    print(f"Configured source {name!r} using {plugin}.")
    print("Restart the incident agent; rebuild and activate the runtime bundle if one is in use.")


def main(argv: list[str] | None = None) -> None:
    args = parse_arguments(argv)
    using_default_config = args.config is None
    configured_path = bool(os.environ.get("INCIDENT_AGENT_CONFIG"))
    if args.config is None:
        source_checkout = (Path(__file__).resolve().parents[1] / "pyproject.toml").is_file()
        args.config = (
            Path(".agent/config.toml")
            if args.command == "init" and source_checkout and not configured_path
            else default_config_path()
        )
    if args.command == "plugins":
        _print_plugins(as_json=args.as_json)
        return
    if args.command == "connect":
        _connect_command(args)
        return
    if args.command == "eval":
        from .evals import (
            run_evaluations,
            run_holdout_evaluation,
            run_repair_evaluation,
            run_retrieval_evaluation,
        )

        if args.suite == "contracts":
            report = run_evaluations(args.dataset)
        elif args.suite == "repair":
            report = run_repair_evaluation()
        else:
            if not args.dataset:
                raise SystemExit(f"eval --suite {args.suite} requires a JSON or JSONL dataset")
            records = _load_eval_records(args.dataset)
            report = (
                run_holdout_evaluation(records)
                if args.suite == "root-cause"
                else run_retrieval_evaluation(records)
            )
        rendered = json.dumps(report, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8")
        else:
            print(rendered)
        if report.get("failed", 0):
            raise SystemExit(1)
        return
    # Dashboard and execution inspection are read-only over an existing runtime.
    if args.command == "executions":
        run_executions_command(args)
        return
    if args.command == "dashboard":
        config_path = args.config or default_config_path()
        result = run_dashboard_command(args, config_path)
        if result:
            raise SystemExit(result)
        return
    if args.command == "update":
        managed_tools: tuple[str, ...] = ()
        if args.config and args.config.is_file():
            managed_tools = (
                ("seed", "code-review-graph")
                if load_config(args.config, create=False).repositories
                else ()
            )
        result = update_installation(managed_tools=managed_tools)
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        if result.returncode:
            raise SystemExit(result.returncode)
        return
    if args.command in {"doctor", "status"}:
        if args.command == "doctor" and args.install:
            from .tooling import install_uv_tools

            results = install_uv_tools(("seed", "code-review-graph"))
            for result in results:
                print(result.stdout or result.stderr, end="")
        checks = doctor(args.config or default_config_path())
        for check in checks:
            print(f"{'OK' if check.ok else 'FAIL'} {check.name}: {check.message}")
        if args.command == "status":
            try:
                active = load_active_bundle(
                    load_config(args.config or default_config_path(), create=False)
                )
                print(f"active bundle: {active.version if active else 'none'}")
            except Exception as error:
                print(f"active bundle: unavailable ({error})")
        if any(not check.ok for check in checks):
            raise SystemExit(1)
        return
    if args.command == "bundle":
        config_path = args.config or default_config_path()
        config = load_config(config_path, create=False)
        if args.action == "build":
            print(build_bundle(config).version)
        elif args.action == "list":
            for item in list_bundles(config):
                print(item.version)
        elif args.action == "activate":
            if not args.version:
                raise SystemExit("bundle activate requires VERSION")
            print(activate_bundle(config, args.version).version)
        else:
            print(rollback_bundle(config).version)
        return
    config_commands = {
        "init",
        "serve",
        "worker",
        "tui",
        "config",
        "mcp",
        "run",
    }
    if args.command in config_commands:
        args.config = bootstrap(args.config)
    local_config = args.config.parent == (Path.cwd() / ".agent").resolve()
    if args.command == "init" and not local_config:
        config_missing = not args.config.exists()
        config = load_config(args.config)
        if using_default_config and config_missing:
            config.runtime_root = default_runtime_path()
            config.server.require_api_auth = True
        if not config.connectors:
            config.connectors.append(
                ConnectorConfig(name="grafana", purpose="incident", type="webhook")
            )
        save_config(config, args.config)
        print(f"Initialized {args.config}")
        return
    if (
        args.command in config_commands
        and using_default_config
        and not (args.command == "init" and local_config)
    ):
        ensure_user_config(args.config)
    should_initialise = (args.command == "init" and local_config) or (
        args.command
        not in {
            "index",
            "tree",
            "install-repositories",
            "export-systemd-env",
            "service-url",
            "healthcheck",
        }
        and local_config
        and not Path(".agent").is_dir()
    )
    if should_initialise:
        result = initialise_runtime_tree()
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        if not result.succeeded:
            raise SystemExit(result.returncode or 1)
        if args.command == "init":
            # seed creates the runtime files, including an empty config.toml.
            # Persist operational defaults so a fresh install can start intake
            # without first requiring an interactive TUI session.
            config = load_config(args.config)
            if not config.connectors:
                config.connectors.append(
                    ConnectorConfig(name="grafana", purpose="incident", type="webhook")
                )
                save_config(config, args.config)
            if result.stdout:
                print(result.stdout, end="")
            return
    if args.command in {"tui", "config"}:
        run_tui(args.config)
        return
    if args.command == "tree":
        result = capture_structured_tree(args.path, args.out)
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        if not result.succeeded:
            raise SystemExit(result.returncode or 1)
        return
    if args.command == "index":
        result = build_repository_graphs(args.path)
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        if not result.succeeded:
            raise SystemExit(result.returncode or 1)
        return
    if args.command == "install-repositories":
        installed = install_configured_repositories(
            args.config,
            args.source_root,
            args.destination_root,
        )
        for repository in installed:
            print(f"Installed repository: {repository}")
        return
    if args.command == "export-systemd-env":
        from .systemd_env import default_secrets_paths

        secrets_paths = args.secrets or default_secrets_paths()
        export_systemd_environment(
            config_path=args.config,
            output_path=args.output,
            secrets_paths=secrets_paths,
        )
        return
    if args.command == "service-url":
        print(service_base_url(load_config(args.config, create=False)))
        return
    if args.command == "healthcheck":
        config = load_config(args.config, create=False)
        url = local_service_base_url(config).rstrip("/") + "/health"
        deadline = time.monotonic() + args.timeout
        last_error = "service did not respond"
        while time.monotonic() < deadline:
            try:
                # /health includes parallel live probes bounded to five seconds.
                with urllib.request.urlopen(url, timeout=6) as response:  # noqa: S310
                    if 200 <= response.status < 300:
                        return
                    last_error = f"health endpoint returned HTTP {response.status}"
            except (OSError, TimeoutError) as error:
                last_error = str(error)
            time.sleep(0.25)
        raise SystemExit(f"health check failed for {url}: {last_error}")
    # Refuse to start a configured service that cannot satisfy its declared
    # runtime contract.  A missing explicit path is left to Application.build
    # for backwards-compatible config creation; ``doctor`` reports it clearly.
    if args.command in {"serve", "worker", "run", "mcp"} and args.config.is_file():
        try:
            ensure_runtime_tools(args.config)
            require_ready(args.config)
        except RuntimeError as error:
            raise SystemExit(str(error)) from error
    application = Application.build(args.config)
    if args.command in {"serve", "mcp"}:
        server = create_server(application, run_worker=not getattr(args, "no_worker", False))
        uvicorn.run(
            server,
            host=application.config.server.host,
            port=application.config.server.port,
        )
    elif args.command == "worker":
        asyncio.run(_worker(application))
    elif args.command == "run":
        if args.incident is None:
            server = create_server(application)
            uvicorn.run(
                server,
                host=application.config.server.host,
                port=application.config.server.port,
            )
        else:
            asyncio.run(_run_direct(application, args.incident))


if __name__ == "__main__":
    main()

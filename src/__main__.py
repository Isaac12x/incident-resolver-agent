"""Command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
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
from .config import ConnectorConfig, load_config, save_config
from .dashboard.cli import add_dashboard_parser, run_dashboard_command
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
from .server import create_server
from .systemd_env import export_systemd_environment, local_service_base_url, service_base_url
from .tooling import (
    build_repository_graphs,
    capture_structured_tree,
    initialise_runtime_tree,
    install_configured_repositories,
)
from .tui import run_tui


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="incident-agent")
    parser.add_argument("--config", type=Path, default=None)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create or repair the local .agent runtime tree")
    serve = commands.add_parser("serve", help="start the HTTP server")
    serve.add_argument("--no-worker", action="store_true")
    commands.add_parser("worker", help="run only the durable task worker")
    commands.add_parser("tui", aliases=["config"], help="configure the harness")
    commands.add_parser("update", help="update the isolated installation through uv")
    doctor_command = commands.add_parser(
        "doctor", help="validate runtime tools, credentials, and repositories"
    )
    doctor_command.add_argument(
        "--install", action="store_true", help="install missing helper CLIs with uv"
    )
    commands.add_parser("status", help="show readiness and active runtime bundle")
    bundle = commands.add_parser(
        "bundle", help="build, list, activate, or roll back runtime bundles"
    )
    bundle.add_argument("action", choices=("build", "list", "activate", "rollback"))
    bundle.add_argument("version", nargs="?")
    commands.add_parser("mcp", help="serve MCP-compatible HTTP endpoints")
    install_repositories = commands.add_parser(
        "install-repositories",
        help="seed configured local repositories into a deployment runtime",
    )
    install_repositories.add_argument("--source-root", type=Path, required=True)
    install_repositories.add_argument("--destination-root", type=Path, required=True)
    export_env = commands.add_parser(
        "export-systemd-env",
        help="write a systemd EnvironmentFile from TUI config and secret stores",
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
    commands.add_parser(
        "service-url",
        help="print the health-check URL from server settings in config",
    )
    healthcheck = commands.add_parser(
        "healthcheck",
        help="wait for the configured HTTP service to become ready",
    )
    healthcheck.add_argument("--timeout", type=float, default=30.0)
    run = commands.add_parser("run", help="submit an incident JSON file, or start the server")
    run.add_argument("incident", type=Path, nargs="?")
    eval_command = commands.add_parser("eval", help="run the packaged evaluation dataset")
    eval_command.add_argument(
        "--suite",
        choices=("contracts", "repair", "root-cause", "retrieval"),
        default="contracts",
    )
    eval_command.add_argument("dataset", type=Path, nargs="?")
    eval_command.add_argument("--output", type=Path)
    index = commands.add_parser("index", help="build the code-review-graph index")
    index.add_argument("path", type=Path, nargs="?", default=Path("."))
    tree = commands.add_parser("tree", help="capture a structured tree with seed-cli")
    tree.add_argument("path", type=Path, nargs="?", default=Path("."))
    tree.add_argument("--out", type=Path, default=Path("structure.seed"))
    add_dashboard_parser(commands)
    return parser.parse_args(argv)


async def _worker(application: Application) -> None:
    await application.connectors.start()
    try:
        await application.workflow.run_worker()
    finally:
        await application.connectors.stop()


async def _run_direct(application: Application, path: Path) -> None:
    incident = Incident.model_validate_json(path.read_text(encoding="utf-8"))
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
    # Dashboard startup is deliberately before bootstrap, readiness checks, and
    # Application.build: it is a read-only process over an existing runtime.
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

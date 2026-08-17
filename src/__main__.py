"""Command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import uvicorn

from .app import Application
from .models import Incident, TaskState
from .server import create_server
from .systemd_env import export_systemd_environment, service_base_url
from .tooling import build_repository_graphs, capture_structured_tree, initialise_runtime_tree
from .tui import run_tui


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="incident-agent")
    parser.add_argument("--config", type=Path, default=Path(".agent/config.toml"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create or repair the local .agent runtime tree")
    serve = commands.add_parser("serve", help="start the HTTP server")
    serve.add_argument("--no-worker", action="store_true")
    commands.add_parser("worker", help="run only the durable task worker")
    commands.add_parser("tui", help="configure the harness")
    commands.add_parser("mcp", help="serve MCP-compatible HTTP endpoints")
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
    run = commands.add_parser("run", help="submit an incident JSON file")
    run.add_argument("incident", type=Path)
    index = commands.add_parser("index", help="build graphify and code-review-graph indexes")
    index.add_argument("path", type=Path, nargs="?", default=Path("."))
    tree = commands.add_parser("tree", help="capture a structured tree with seed-cli")
    tree.add_argument("path", type=Path, nargs="?", default=Path("."))
    tree.add_argument("--out", type=Path, default=Path("structure.seed"))
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


def main(argv: list[str] | None = None) -> None:
    args = parse_arguments(argv)
    should_initialise = args.command == "init" or (
        args.command not in {"index", "tree", "export-systemd-env", "service-url"}
        and not Path(".agent").is_dir()
    )
    if should_initialise:
        result = initialise_runtime_tree()
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        if not result.succeeded:
            raise SystemExit(result.returncode or 1)
        if args.command == "init":
            if result.stdout:
                print(result.stdout, end="")
            return
    if args.command == "tui":
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
        results = build_repository_graphs(args.path)
        for result in results:
            if result.stdout:
                print(result.stdout, end="")
            if result.stderr:
                print(result.stderr, end="", file=sys.stderr)
        if any(not result.succeeded for result in results):
            code = next(result.returncode for result in results if result.returncode) or 1
            raise SystemExit(code)
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
        from .config import load_config

        print(service_base_url(load_config(args.config, create=False)))
        return
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
        asyncio.run(_run_direct(application, args.incident))


if __name__ == "__main__":
    main()

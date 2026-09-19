"""Dashboard command parsing and dispatch."""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from .process import DashboardError, DashboardProcess, _start_identity, canonical_runtime_root


def _listen_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("listen port must be an integer") from error
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("listen port must be between 1 and 65535")
    return port


def add_dashboard_parser(commands: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = commands.add_parser(
        "dashboard",
        help="serve the read-only incident dashboard",
        description=(
            "Live read-only view of the selected runtime. Does not start repair workers. "
            "Omit ACTION to start in the foreground."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "actions:\n"
            "  (default)     start in the foreground and print the loopback URL\n"
            "  -d            start detached; survives terminal exit\n"
            "  status        process, URL, public route, snapshot check time\n"
            "  stop          stop the dashboard only\n"
            "  logs          recent dashboard logs\n"
            "  port PORT     publish through the host reverse proxy\n"
            "  port close    revoke the public route\n"
            "\n"
            "examples:\n"
            "  incident-agent dashboard\n"
            "  incident-agent dashboard -d\n"
            "  incident-agent dashboard status\n"
            "  incident-agent dashboard port 8443 --host incidents.example.com\n"
        ),
    )
    parser.add_argument(
        "action",
        nargs="?",
        help="status, stop, logs, port, or omit to start",
    )
    parser.add_argument("target", nargs="?", help="PORT for 'port', or 'close'")
    parser.add_argument("-d", "--detached", "--dettached", action="store_true", dest="detached")
    parser.add_argument("--listen-port", type=_listen_port, default=8766)
    parser.add_argument("--host")
    parser.add_argument("--proxy")
    parser.add_argument("--proxy-config", type=Path)
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    parser.add_argument("--route-path", default="/")
    parser.add_argument("--_internal", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_nonce", default="", help=argparse.SUPPRESS)
    return parser


def run_dashboard_command(args: argparse.Namespace, config_path: Path) -> int:
    internal = bool(getattr(args, "_internal", False))
    if internal:
        if (
            args.action not in (None, "")
            or getattr(args, "target", None) is not None
            or not getattr(args, "_nonce", "")
        ):
            raise SystemExit("invalid internal dashboard invocation")
        if args.detached:
            raise SystemExit("invalid internal dashboard invocation")
    elif (
        hasattr(os, "geteuid")
        and os.geteuid() == 0
        and args.action not in {"status", "stop", "logs", "port"}
    ):
        raise SystemExit("dashboard refuses to run as root")
    if internal and hasattr(os, "geteuid") and os.geteuid() == 0:
        raise SystemExit("dashboard refuses to run as root")
    if not internal:
        action = getattr(args, "action", None)
        target = getattr(args, "target", None)
        if action in {"status", "stop", "logs"} and target is not None:
            raise SystemExit(f"dashboard {action} does not accept a target")
        if action == "port" and target is None:
            raise SystemExit("dashboard port requires PORT or close")
        if action not in {None, "", "status", "stop", "logs", "port"}:
            raise SystemExit(f"unknown dashboard action: {action}")
    root = canonical_runtime_root(config_path)
    process = DashboardProcess(root, port=args.listen_port, config=config_path)
    if internal:
        # The hidden server entry point is only valid for the child recorded by
        # DashboardProcess.start().  A nonce by itself must not permit an
        # arbitrary process to start a dashboard listener.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            state = process.read_state()
            if (
                state
                and state.get("nonce") == args._nonce
                and state.get("pid") == os.getpid()
                and state.get("pid_start_identity") == _start_identity(os.getpid())
            ):
                break
            time.sleep(0.01)
        else:
            raise SystemExit("invalid internal dashboard identity")
        import uvicorn

        from .web import create_dashboard

        app = create_dashboard(root, port=args.listen_port, nonce=args._nonce)
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=args.listen_port,
            log_level="info",
            proxy_headers=False,
        )
        return 0
    if args.action == "status":
        state = process.existing()
        if not state:
            print("dashboard: stopped")
            return 1
        ready = process.wait_ready(state, timeout=2)
        public = process.directory / "public.json"
        route = "closed"
        freshness = "unavailable"
        try:
            from .data import RuntimeReader

            snapshot = RuntimeReader(root).snapshot(page=1, page_size=1)
            if snapshot.get("available"):
                freshness = str(snapshot.get("refreshed_at", "available"))
        except (OSError, ValueError, TypeError):
            pass
        try:
            import json

            metadata = json.loads(public.read_text(encoding="utf-8"))
            if isinstance(metadata, dict) and metadata.get("enabled"):
                route = f"https://{metadata.get('host', '')}:{metadata.get('port', '')}"
        except (OSError, ValueError):
            pass
        print(
            f"dashboard: {'ready' if ready else 'starting'} "
            f"pid={state['pid']} url=http://127.0.0.1:{state['port']} "
            f"public={route} snapshot_checked_at={freshness}"
        )
        return 0 if ready else 1
    if args.action == "stop":
        try:
            process.stop()
        except (DashboardError, OSError) as error:
            raise SystemExit(str(error)) from error
        # Route ownership is revoked before adapter cleanup, when available.
        print("dashboard: stopped")
        return 0
    if args.action == "logs":
        print(process.logs())
        return 0
    if args.action == "port":
        from .routing import run_port_command

        running = process.existing()
        if args.target != "close" and (
            not running or not running.get("detached") or not process.wait_ready(running, timeout=2)
        ):
            raise SystemExit(
                "dashboard port requires a running detached instance; "
                "start with 'incident-agent dashboard -d'"
            )
        if running and args.target != "close":
            args.upstream_port = running["port"]
        try:
            return run_port_command(args, root)
        except (DashboardError, OSError, RuntimeError) as error:
            raise SystemExit(str(error)) from error
    if args.action not in (None, ""):
        raise SystemExit(f"unknown dashboard action: {args.action}")
    existing = process.existing()
    if existing:
        if not process.wait_ready(existing, timeout=2):
            raise SystemExit("dashboard process exists but is not ready")
        print(f"Dashboard already running at http://127.0.0.1:{existing['port']}")
        return 0
    try:
        state = process.start(detached=args.detached)
    except (DashboardError, OSError) as error:
        raise SystemExit(str(error)) from error
    print(
        f"Dashboard running at http://127.0.0.1:{state['port']} "
        f"(token: {root / 'dashboard' / 'token'})"
    )
    if args.detached:
        return 0
    import uvicorn

    from .web import create_dashboard

    app = create_dashboard(root, port=args.listen_port, nonce=state["nonce"])
    try:
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=args.listen_port,
            log_level="info",
            proxy_headers=False,
        )
    finally:
        process.stop_foreground()
    return 0

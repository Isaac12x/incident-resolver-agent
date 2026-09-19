"""Grouped command catalog and help formatting for the incident-agent CLI."""

from __future__ import annotations

import argparse
import difflib
import sys

PROG = "incident-agent"
DESCRIPTION = (
    "Durable agent harness that turns production incidents into locally tested, "
    "deployment-verified pull requests."
)

HELP = {
    "init": "create or repair the local runtime",
    "config": "open the configuration editor",
    "plugins": "list built-in source adapters",
    "connect": "configure a named source adapter",
    "doctor": "validate tools, credentials, and repositories",
    "status": "show readiness and the active runtime bundle",
    "run": "submit incident JSON, or start the HTTP server",
    "serve": "start the HTTP server",
    "worker": "run only the durable task worker",
    "mcp": "serve MCP-compatible HTTP endpoints",
    "dashboard": "serve the read-only incident dashboard",
    "executions": "list or inspect previous agent runs",
    "update": "update the isolated installation",
    "bundle": "build, list, activate, or roll back runtime bundles",
    "healthcheck": "wait for the HTTP service to become ready",
    "service-url": "print the configured health-check URL",
    "eval": "run the packaged evaluation dataset",
    "index": "build the code-review-graph index",
    "tree": "capture a structured tree with seed-cli",
    "install-repositories": "seed configured repositories into a runtime",
    "export-systemd-env": "write a systemd EnvironmentFile",
}

GROUPS = (
    ("Setup", ("init", "config", "plugins", "connect", "doctor", "status")),
    ("Run", ("run", "serve", "worker", "mcp", "dashboard", "executions")),
    ("Maintain", ("update", "bundle", "healthcheck", "service-url")),
    ("Tools", ("eval", "index", "tree")),
    ("Deploy", ("install-repositories", "export-systemd-env")),
)

DISPLAY = {"config": "config, tui"}
ALIASES = {"tui": "config"}
EXAMPLES = (
    "incident-agent init",
    "incident-agent config",
    "incident-agent doctor",
    "incident-agent run",
    "incident-agent dashboard",
    "incident-agent executions list",
    "incident-agent connect --list",
    "incident-agent COMMAND --help",
)


def known_commands() -> tuple[str, ...]:
    names = list(HELP)
    names.extend(ALIASES)
    return tuple(names)


def format_cli_help() -> str:
    lines: list[str] = []
    for title, names in GROUPS:
        lines.append(title)
        for name in names:
            label = DISPLAY.get(name, name)
            lines.append(f"  {label:<24}{HELP[name]}")
        lines.append("")
    lines.append("Examples")
    lines.extend(f"  {example}" for example in EXAMPLES)
    lines.append("")
    lines.append("Run 'incident-agent COMMAND --help' for command options.")
    return "\n".join(lines)


def overview_next_steps(*, ready: bool) -> str:
    if ready:
        return "\n".join(
            (
                "Ready. Start intake or open the live dashboard:",
                "  incident-agent run",
                "  incident-agent dashboard",
                "  incident-agent doctor",
            )
        )
    return "\n".join(
        (
            "Fix the failing checks, Save, then recheck:",
            "  incident-agent doctor",
            "  incident-agent connect",
            "  incident-agent COMMAND --help",
        )
    )


def suggest_command(value: str) -> str | None:
    matches = difflib.get_close_matches(value, known_commands(), n=3, cutoff=0.45)
    if not matches:
        return None
    if len(matches) == 1:
        return f"Did you mean {matches[0]}?"
    return "Did you mean " + ", ".join(matches[:-1]) + f", or {matches[-1]}?"


class HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Keep usage short and leave command grouping to the epilog."""

    def _format_action(self, action: argparse.Action) -> str:
        if isinstance(action, argparse._SubParsersAction):
            return ""
        return super()._format_action(action)


class AgentParser(argparse.ArgumentParser):
    """Print grouped help when no command is given; hint on unknown commands."""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        hint = f"Try '{self.prog} --help' for commands."
        if "invalid choice:" in message:
            start = message.find("'")
            end = message.find("'", start + 1)
            choice = message[start + 1 : end] if start != -1 and end != -1 else ""
            suggestion = suggest_command(choice) if choice else None
            if self.prog == PROG and choice:
                message = f"unknown command {choice!r}"
            if suggestion:
                hint = f"{suggestion} {hint}"
        self.exit(2, f"{self.prog}: {message}\n{hint}\n")

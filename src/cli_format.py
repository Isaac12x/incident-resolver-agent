"""Small, dependency-free helpers for readable terminal output."""

from __future__ import annotations

import os
import re
import shutil
import sys
import textwrap
from collections.abc import Iterable, Sequence
from contextlib import suppress
from typing import TextIO

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def supports_color(stream: TextIO | None = None) -> bool:
    """Whether ANSI color should be used for this output stream."""
    if stream is None:
        stream = sys.stdout
    if "NO_COLOR" in os.environ or os.environ.get("TERM", "") == "dumb":
        return False
    force = os.environ.get("FORCE_COLOR")
    if force is not None:
        return force not in {"", "0"}
    return bool(stream.isatty())


def color(text: object, code: str, stream: TextIO | None = None) -> str:
    """Wrap text in an ANSI SGR code when the target stream supports color."""
    value = str(text)
    if not supports_color(stream):
        return value
    return f"\033[{code}m{value}\033[0m"


def render_table(
    headers: Sequence[object], rows: Iterable[Sequence[object]], stream: TextIO | None = None
) -> str:
    """Render a compact, dependency-free table with aligned columns."""
    values = [[str(item) for item in row] for row in rows]
    columns = len(headers)
    if any(len(row) != columns for row in values):
        raise ValueError("each table row must match the number of headers")
    labels = [str(header) for header in headers]
    widths = [len(_ANSI.sub("", label)) for label in labels]
    for row in values:
        for index, item in enumerate(row):
            lengths = [len(_ANSI.sub("", part)) for part in item.splitlines()]
            widths[index] = max([widths[index], *lengths])
    if widths:
        terminal_width = shutil.get_terminal_size().columns
        if stream is not None:
            with suppress(AttributeError, OSError):
                terminal_width = os.get_terminal_size(stream.fileno()).columns
        fixed_width = sum(widths[:-1]) + 2 * (len(widths) - 1)
        widths[-1] = min(widths[-1], max(20, min(80, terminal_width - fixed_width - 2)))
    lines = ["  ".join(_pad(label, widths[index]) for index, label in enumerate(labels))]
    lines.append("  ".join("─" * width for width in widths))
    for row in values:
        parts = []
        for index, item in enumerate(row):
            cell_lines = []
            for source_line in item.splitlines() or [""]:
                if index == len(row) - 1:
                    cell_lines.extend(textwrap.wrap(source_line, width=widths[index]) or [""])
                else:
                    cell_lines.append(source_line)
            parts.append(cell_lines)
        for line_index in range(max(map(len, parts), default=1)):
            lines.append(
                "  ".join(
                    _pad(part[line_index] if line_index < len(part) else "", widths[index])
                    for index, part in enumerate(parts)
                )
            )
    return "\n".join(line.rstrip() for line in lines)


def _pad(value: str, width: int) -> str:
    return value + " " * max(0, width - len(_ANSI.sub("", value)))

"""Repository documentation discovery and repair-context assembly."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

_IGNORED_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "__pycache__",
    "node_modules",
    "vendor",
    "dist",
    "build",
}


@dataclass(frozen=True)
class RepositoryDocumentation:
    """Documentation found for one repository or its application workspace."""

    label: str
    root: Path
    vision_path: Path | None
    vision: str
    architecture_paths: tuple[Path, ...]
    architecture: tuple[tuple[Path, str], ...]
    vision_status: str = "absent"
    omissions: tuple[str, ...] = ()

    @property
    def has_vision(self) -> bool:
        return self.vision_path is not None

    @property
    def has_architecture(self) -> bool:
        return bool(self.architecture_paths)


_MAX_DOCUMENT_BYTES = 512 * 1024
_MAX_CONTEXT_BYTES = 128 * 1024
_MAX_ARCHITECTURE_DOCUMENTS = 32
_MAX_OMISSIONS = 32


def _is_document_candidate(path: Path, root: Path) -> bool:
    name = path.name.casefold()
    if name == "vision.md" or name in {"architecture.md", "architecture.mdx", "architecture.rst"}:
        return True
    relative_parts = path.relative_to(root).parts
    return any(part.casefold() in {"adr", "architecture"} for part in relative_parts[:-1]) and (
        path.suffix.casefold() in {".md", ".mdx", ".rst"}
    )


def _files(root: Path, excluded_roots: tuple[Path, ...] = ()) -> tuple[list[Path], list[str]]:
    if not root.is_dir():
        return [], []
    found: list[Path] = []
    omissions: list[str] = []
    excluded = tuple(path.resolve() for path in excluded_roots)
    for directory, directories, filenames in os.walk(root, followlinks=False):
        current = Path(directory).resolve()
        directories[:] = [
            name
            for name in directories
            if name.casefold() not in _IGNORED_DIRECTORIES
            and (current / name).resolve() not in excluded
        ]
        for name in filenames:
            path = current / name
            if not _is_document_candidate(path, root):
                continue
            try:
                resolved = path.resolve()
                resolved.relative_to(root)
                if any(
                    resolved == excluded_root or excluded_root in resolved.parents
                    for excluded_root in excluded
                ):
                    continue
                info = path.stat()
                if not stat.S_ISREG(info.st_mode):
                    omissions.append(f"{path.relative_to(root)} (not a regular file)")
                    continue
                if info.st_size <= _MAX_DOCUMENT_BYTES:
                    found.append(path)
                else:
                    omissions.append(f"{path.relative_to(root)} (larger than 512 KiB)")
            except (OSError, ValueError):
                omissions.append(f"{path.relative_to(root)} (unreadable)")
    return (
        sorted(found, key=lambda path: path.relative_to(root).as_posix().casefold()),
        omissions[:_MAX_OMISSIONS],
    )


def discover_documentation(
    root: Path,
    label: str = "repository",
    *,
    excluded_roots: tuple[Path, ...] = (),
) -> RepositoryDocumentation:
    """Find vision and architecture docs case-insensitively without inventing either."""
    root = root.resolve()
    files, omissions = _files(root, excluded_roots)
    vision_candidates = sorted(
        (path for path in files if path.name.casefold() == "vision.md"),
        key=lambda path: (
            len(path.relative_to(root).parts),
            path.relative_to(root).as_posix().casefold(),
        ),
    )
    vision_path = vision_candidates[0] if vision_candidates else None
    vision_status = "absent"
    vision = ""
    if vision_path:
        try:
            vision = vision_path.read_text(encoding="utf-8")
            vision_status = "loaded"
        except (OSError, UnicodeError):
            vision_status = "unreadable"
            omissions.append(f"{vision_path.relative_to(root)} (unreadable)")
    if len(vision.encode("utf-8")) > _MAX_CONTEXT_BYTES:
        omissions.append(f"{vision_path.relative_to(root)} (context size limit)")
        vision = ""
        vision_status = "omitted"

    architecture: list[Path] = []
    for path in files:
        name = path.name.casefold()
        relative_parts = path.relative_to(root).parts
        in_adr = any(part.casefold() in {"adr", "architecture"} for part in relative_parts[:-1])
        is_architecture_file = name in {"architecture.md", "architecture.mdx", "architecture.rst"}
        if is_architecture_file or (in_adr and path.suffix.casefold() in {".md", ".mdx", ".rst"}):
            architecture.append(path)
    architecture = sorted(
        architecture,
        key=lambda path: path.relative_to(root).as_posix().casefold(),
    )
    if len(architecture) > _MAX_ARCHITECTURE_DOCUMENTS:
        for path in architecture[_MAX_ARCHITECTURE_DOCUMENTS:]:
            omissions.append(f"{path.relative_to(root)} (architecture document limit)")
        architecture = architecture[:_MAX_ARCHITECTURE_DOCUMENTS]
    loaded: list[tuple[Path, str]] = []
    context_bytes = len(vision.encode("utf-8"))
    for path in architecture:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            omissions.append(f"{path.relative_to(root)} (unreadable)")
            continue
        content_bytes = len(content.encode("utf-8"))
        if context_bytes + content_bytes > _MAX_CONTEXT_BYTES:
            omissions.append(f"{path.relative_to(root)} (context size limit)")
            continue
        loaded.append((path, content))
        context_bytes += content_bytes
    return RepositoryDocumentation(
        label=label,
        root=root,
        vision_path=vision_path,
        vision=vision,
        architecture_paths=tuple(architecture),
        architecture=tuple(loaded),
        vision_status=vision_status,
        omissions=tuple(omissions[:_MAX_OMISSIONS]),
    )


def render_documentation_context(
    contexts: tuple[RepositoryDocumentation, ...],
) -> str:
    """Render explicit present/absent documentation state for the agent prompt."""
    sections: list[str] = [
        "# Repository Vision and Architecture Context\n\n"
        "Documentation is evidence about project intent. Use it when present and do not infer "
        "requirements from a missing document. Each section below is scoped to its labeled root."
    ]
    for context in contexts:
        sections.append(f"## {context.label}\n\nRoot: `{context.root}`")
        vision_omitted = any("vision.md" in item.casefold() for item in context.omissions)
        architecture_omitted = any(
            "architecture" in item.casefold() or "adr" in item.casefold()
            for item in context.omissions
        )
        if context.vision_status == "loaded":
            sections.append(
                f"### Vision ({context.vision_path.relative_to(context.root)})\n\n{context.vision}"
            )
        elif context.vision_status == "unreadable" or vision_omitted:
            vision_label = (
                str(context.vision_path.relative_to(context.root))
                if context.vision_path
                else "VISION.md"
            )
            sections.append(
                f"### Vision ({vision_label})\n\n"
                "A `VISION.md` was found but could not be read. Do not treat it as absent; "
                "report the documentation access problem if its constraints matter."
            )
        else:
            sections.append(
                "### Vision\n\nNo `VISION.md` was found in this scope. Do not invent or assume "
                "vision constraints."
            )
        if context.architecture:
            for path, content in context.architecture:
                sections.append(
                    f"### Architecture ({path.relative_to(context.root)})\n\n{content}"
                )
        elif context.architecture_paths or architecture_omitted:
            sections.append(
                "### Architecture\n\nArchitecture documentation was found but could not be "
                "loaded. Do not treat it as absent; report the documentation access problem if "
                "its constraints matter."
            )
        else:
            sections.append(
                "### Architecture\n\nNo architecture document or ADR was found in this scope. "
                "Do not invent architecture constraints. If a fix truly requires an architectural "
                "decision, explain why and create a small ADR in the repository when appropriate."
            )
        if context.omissions:
            sections.append(
                "### Documentation omitted\n\n"
                + "\n".join(f"- {item}" for item in context.omissions)
            )
    return "\n\n".join(sections)

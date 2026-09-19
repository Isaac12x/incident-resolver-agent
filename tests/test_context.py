from pathlib import Path

from src.context import (
    RepositoryDocumentation,
    discover_documentation,
    render_documentation_context,
)


def test_documentation_discovery_is_case_insensitive_and_scoped(tmp_path: Path) -> None:
    (tmp_path / "ViSiOn.md").write_text("Ship useful repairs.\n", encoding="utf-8")
    (tmp_path / "Docs" / "ArChItEcTuRe").mkdir(parents=True)
    (tmp_path / "Docs" / "ArChItEcTuRe" / "decision.MDX").write_text(
        "Use a queue.\n", encoding="utf-8"
    )
    (tmp_path / "Docs" / "adr").mkdir(parents=True)
    (tmp_path / "Docs" / "adr" / "0001.md").write_text("Record changes.\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "VISION.md").write_text("ignore", encoding="utf-8")

    context = discover_documentation(tmp_path, "checkout")

    assert context.has_vision
    assert context.vision == "Ship useful repairs.\n"
    assert {
        path.relative_to(tmp_path).as_posix().casefold() for path in context.architecture_paths
    } == {
        "docs/architecture/decision.mdx",
        "docs/adr/0001.md",
    }


def test_root_vision_wins_and_symlinked_docs_do_not_escape_scope(tmp_path: Path) -> None:
    (tmp_path / "VISION.md").write_text("root", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "vision.md").write_text("nested", encoding="utf-8")
    outside = tmp_path.parent / "outside-vision.md"
    outside.write_text("outside", encoding="utf-8")
    (tmp_path / "linked-vision.md").symlink_to(outside)

    context = discover_documentation(tmp_path)

    assert context.vision == "root"
    assert context.vision_path == tmp_path / "VISION.md"


def test_missing_documentation_is_explicit_and_does_not_create_constraints(tmp_path: Path) -> None:
    rendered = render_documentation_context((discover_documentation(tmp_path, "repository"),))

    assert "No `VISION.md` was found" in rendered
    assert "No architecture document or ADR was found" in rendered
    assert "Do not invent or assume vision constraints" in rendered
    assert "Do not invent architecture constraints" in rendered


def test_oversized_document_is_reported_as_omitted_not_absent(tmp_path: Path) -> None:
    (tmp_path / "VISION.md").write_text("x" * (512 * 1024 + 1), encoding="utf-8")
    (tmp_path / "ARCHITECTURE.md").write_text("small", encoding="utf-8")

    context = discover_documentation(tmp_path)
    rendered = render_documentation_context((context,))

    assert "found but could not be read" in rendered
    assert "No `VISION.md` was found" not in rendered
    assert "VISION.md (larger than 512 KiB)" in rendered


def test_vision_context_limit_is_reported_and_bounds_architecture_context(tmp_path: Path) -> None:
    (tmp_path / "VISION.md").write_text("x" * (128 * 1024 + 1), encoding="utf-8")
    (tmp_path / "ARCHITECTURE.md").write_text("architecture", encoding="utf-8")

    context = discover_documentation(tmp_path)
    rendered = render_documentation_context((context,))

    assert context.vision_status == "omitted"
    assert context.vision == ""
    assert "VISION.md (context size limit)" in rendered
    assert "Architecture (ARCHITECTURE.md)" in rendered


def test_non_regular_document_is_reported_as_unreadable(tmp_path: Path) -> None:
    fifo = tmp_path / "VISION.md"
    try:
        fifo.mkfifo()
    except (AttributeError, NotImplementedError, OSError):
        return
    context = discover_documentation(tmp_path)
    rendered = render_documentation_context((context,))

    assert context.vision_status == "absent"
    assert "VISION.md (not a regular file)" in rendered
    assert "Architecture documentation was found" not in rendered


def test_application_and_repository_contexts_render_separately(tmp_path: Path) -> None:
    app = tmp_path / "application"
    repo = app / "api"
    repo.mkdir(parents=True)
    (app / "VISION.md").write_text("Application intent", encoding="utf-8")
    (repo / "ARCHITECTURE.md").write_text("Repository design", encoding="utf-8")

    rendered = render_documentation_context(
        (discover_documentation(app, "application"), discover_documentation(repo, "api"))
    )

    assert "## application" in rendered
    assert "Application intent" in rendered
    assert "## api" in rendered
    assert "Repository design" in rendered


def test_excluded_roots_and_architecture_context_limit_are_respected(tmp_path: Path) -> None:
    excluded = tmp_path / "nested"
    excluded.mkdir()
    (excluded / "ARCHITECTURE.md").write_text("excluded", encoding="utf-8")
    (tmp_path / "ARCHITECTURE.md").write_text("a" * (128 * 1024 + 1), encoding="utf-8")

    context = discover_documentation(tmp_path, excluded_roots=(excluded,))

    assert all(path.parent != excluded for path in context.architecture_paths)
    assert any("context size limit" in omission for omission in context.omissions)


def test_architecture_document_limit_and_non_directory_root(tmp_path: Path) -> None:
    for index in range(33):
        directory = tmp_path / "architecture" / f"{index:02d}"
        directory.mkdir(parents=True)
        (directory / "decision.md").write_text(str(index), encoding="utf-8")
    context = discover_documentation(tmp_path)
    assert context.has_architecture
    assert len(context.architecture_paths) == 32
    assert any("architecture document limit" in item for item in context.omissions)
    assert not discover_documentation(tmp_path / "missing").has_architecture


def test_broken_link_is_reported_as_unreadable(tmp_path: Path) -> None:
    (tmp_path / "VISION.md").symlink_to(tmp_path / "missing-target")
    context = discover_documentation(tmp_path)
    assert any("VISION.md (unreadable)" in item for item in context.omissions)


def test_render_reports_architecture_that_cannot_be_loaded(tmp_path: Path) -> None:
    context = RepositoryDocumentation(
        label="repository",
        root=tmp_path,
        vision_path=None,
        vision="",
        architecture_paths=(tmp_path / "ARCHITECTURE.md",),
        architecture=(),
        omissions=(),
    )
    rendered = render_documentation_context((context,))
    assert "Architecture documentation was found but could not be loaded" in rendered


def test_unreadable_architecture_document_is_omitted(tmp_path: Path, monkeypatch) -> None:
    document = tmp_path / "ARCHITECTURE.md"
    document.write_text("secret", encoding="utf-8")
    original_read_text = Path.read_text

    def fail_architecture(path: Path, *args, **kwargs):
        if path == document:
            raise OSError("permission denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_architecture)
    context = discover_documentation(tmp_path)
    assert any("ARCHITECTURE.md (unreadable)" in item for item in context.omissions)

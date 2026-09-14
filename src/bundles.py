"""Immutable, secret-safe runtime bundles.

Bundles make the inputs used by an agent run reviewable and reversible.  A bundle
contains copies of configuration inputs and a manifest; values of environment
variables are deliberately never read or written.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .config import Config, load_config


@dataclass(frozen=True)
class Bundle:
    version: str
    path: Path
    manifest: dict[str, Any]

    @property
    def config_path(self) -> Path:
        return self.path / "config.json"


def _safe_snapshot(config: Config) -> dict[str, Any]:
    data = config.model_dump(mode="json")
    _reject_inline_credentials(data)
    # Config stores references (for example OPENAI_API_KEY), never secret values.
    return data


def _reject_inline_credentials(value: Any) -> None:
    """Reject common inline credential forms; credentials belong in env stores."""
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(child, str) and (key in {"url", "clone_url"} and "://" in child):
                scheme, _, rest = child.partition("://")
                if "@" in rest.split("/", 1)[0]:
                    raise ValueError(f"inline credentials are not allowed in {key}")
                if any(
                    name in parse_qs(urlsplit(child).query)
                    for name in ("token", "key", "secret", "password", "api_key")
                ):
                    raise ValueError(f"inline credentials are not allowed in {key}")
            if (
                key in {"command", "args"}
                and isinstance(child, list)
                and any(
                    "=" in str(item)
                    and str(item).split("=", 1)[0].lower()
                    in {"token", "password", "secret", "api_key"}
                    for item in child
                )
            ):
                raise ValueError("inline credentials are not allowed in command arguments")
            _reject_inline_credentials(child)
    elif isinstance(value, list):
        for child in value:
            _reject_inline_credentials(child)


def _environment_refs(config: Config) -> list[str]:
    refs: set[str] = set()
    for value in _safe_snapshot(config).values():
        _collect_refs(value, refs)
    return sorted(refs)


def _collect_refs(value: Any, result: set[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.endswith("_env") and isinstance(child, str) and child:
                result.add(child)
            _collect_refs(child, result)
    elif isinstance(value, list):
        for child in value:
            _collect_refs(child, result)


def _fingerprint(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def bundle_root(config: Config, path: Path | str | None = None) -> Path:
    return (
        Path(path).expanduser().resolve()
        if path
        else Path(config.runtime_root).expanduser().resolve() / "bundles"
    )


def build_bundle(config: Config | Path | str, path: Path | str | None = None) -> Bundle:
    """Build a deterministic bundle, returning an existing one for identical inputs."""
    loaded = load_config(Path(config), create=False) if isinstance(config, (str, Path)) else config
    snapshot = _safe_snapshot(loaded)
    skills: dict[str, str] = {}
    directories = list(loaded.agent.skill_directories)
    builtin_candidates = [
        Path(__file__).resolve().parent / "builtin_skills",
        Path(__file__).resolve().parents[1] / "skills",
    ]
    for builtin in builtin_candidates:
        if builtin.is_dir() and str(builtin) not in directories:
            directories.append(str(builtin))
    for directory_index, directory in enumerate(directories):
        root = Path(directory)
        if not root.is_absolute():
            root = Path.cwd() / root
        if root.is_dir():
            for file in sorted(root.rglob("SKILL.md")):
                if file.is_file() and file.stat().st_size <= 2_000_000:
                    skills[f"{directory_index}/{file.relative_to(root)}"] = file.read_text(
                        encoding="utf-8", errors="replace"
                    )
    payload = {"config": snapshot, "skills": skills, "environment_refs": _environment_refs(loaded)}
    version = _fingerprint(payload)
    root = bundle_root(loaded, path)
    destination = root / version
    if destination.is_dir():
        return Bundle(version, destination, json.loads((destination / "manifest.json").read_text()))
    root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{version}.", dir=root))
    try:
        (temporary / "config.json").write_text(
            json.dumps(snapshot, sort_keys=True, indent=2) + "\n"
        )
        (temporary / "skills.json").write_text(json.dumps(skills, sort_keys=True, indent=2) + "\n")
        (temporary / "prompt.txt").write_text(loaded.agent.system_prompt, encoding="utf-8")
        (temporary / "connections.json").write_text(
            json.dumps(snapshot.get("connectors", []), sort_keys=True, indent=2) + "\n"
        )
        skills_root = temporary / "skills"
        for relative, content in skills.items():
            target = skills_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        manifest = {
            "version": version,
            "environment_refs": payload["environment_refs"],
            "files": ["config.json", "connections.json", "prompt.txt", "skills.json"],
        }
        checksum_files = list(manifest["files"])
        checksum_files.extend(
            str(file.relative_to(temporary))
            for file in sorted((temporary / "skills").rglob("*"))
            if file.is_file()
        )
        manifest["files"] = checksum_files
        manifest["checksums"] = {
            name: hashlib.sha256((temporary / name).read_bytes()).hexdigest()
            for name in checksum_files
        }
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n"
        )
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return Bundle(version, destination, manifest)


def list_bundles(config: Config | Path | str, path: Path | str | None = None) -> list[Bundle]:
    loaded = load_config(Path(config), create=False) if isinstance(config, (str, Path)) else config
    root = bundle_root(loaded, path)
    result = []
    for child in sorted(root.iterdir()) if root.is_dir() else []:
        manifest = child / "manifest.json"
        if child.is_dir() and manifest.is_file():
            try:
                item = Bundle(child.name, child, json.loads(manifest.read_text()))
                _verify_bundle(item)
            except (OSError, ValueError, KeyError):
                continue
            result.append(item)
    return result


def _verify_bundle(bundle: Bundle) -> None:
    checksums = bundle.manifest.get("checksums")
    files = bundle.manifest.get("files")
    if not isinstance(checksums, dict) or not isinstance(files, list):
        raise ValueError(f"bundle manifest is incomplete: {bundle.version}")
    allowed = set(files)
    actual = {
        str(file.relative_to(bundle.path))
        for file in bundle.path.rglob("*")
        if file.is_file() and file.name != "manifest.json"
    }
    if any(Path(name).is_absolute() or ".." in Path(name).parts for name in allowed):
        raise ValueError(f"bundle manifest contains an unsafe path: {bundle.version}")
    if actual != allowed:
        raise ValueError(f"bundle contents differ from manifest: {bundle.version}")
    if any(file.is_symlink() for file in bundle.path.rglob("*")):
        raise ValueError(f"bundle contains a symlink: {bundle.version}")
    config = json.loads((bundle.path / "config.json").read_text(encoding="utf-8"))
    skills = json.loads((bundle.path / "skills.json").read_text(encoding="utf-8"))
    canonical = _fingerprint(
        {
            "config": config,
            "skills": skills,
            "environment_refs": bundle.manifest.get("environment_refs", []),
        }
    )
    if canonical != bundle.version:
        raise ValueError(f"bundle version does not match content: {bundle.version}")
    for name in files:
        target = bundle.path / str(name)
        if (
            not target.is_file()
            or checksums.get(name) != hashlib.sha256(target.read_bytes()).hexdigest()
        ):
            raise ValueError(f"bundle content failed verification: {bundle.version}/{name}")


def activate_bundle(
    config: Config | Path | str, version: str, path: Path | str | None = None
) -> Bundle:
    loaded = load_config(Path(config), create=False) if isinstance(config, (str, Path)) else config
    matches = [item for item in list_bundles(loaded, path) if item.version == version]
    if not matches:
        raise FileNotFoundError(f"bundle does not exist: {version}")
    root = bundle_root(loaded, path)
    _verify_bundle(matches[0])
    marker = root / "active"
    history = root / "activation-history.json"
    previous = json.loads(history.read_text()) if history.is_file() else []
    previous.append(version)
    history_tmp = history.with_suffix(".tmp")
    history_tmp.write_text(json.dumps(previous[-50:]) + "\n", encoding="utf-8")
    os.replace(history_tmp, history)
    temporary = marker.with_suffix(".tmp")
    temporary.write_text(version + "\n", encoding="utf-8")
    os.replace(temporary, marker)
    return matches[0]


def load_active_bundle(
    config: Config | Path | str, path: Path | str | None = None
) -> Bundle | None:
    loaded = load_config(Path(config), create=False) if isinstance(config, (str, Path)) else config
    marker = bundle_root(loaded, path) / "active"
    if not marker.is_file():
        return None
    version = marker.read_text(encoding="utf-8").strip()
    selected = next((item for item in list_bundles(loaded, path) if item.version == version), None)
    if selected is None:
        raise RuntimeError(f"active bundle is missing: {version}")
    _verify_bundle(selected)
    return selected


def effective_config(config: Config, path: Path | str | None = None) -> Config:
    """Return the active immutable config snapshot, or the supplied config."""
    active = load_active_bundle(config, path)
    if active is None:
        return config
    return Config.model_validate(json.loads(active.config_path.read_text(encoding="utf-8")))


def rollback_bundle(config: Config | Path | str, path: Path | str | None = None) -> Bundle:
    items = list_bundles(config, path)
    active = load_active_bundle(config, path)
    root = bundle_root(
        load_config(Path(config), create=False) if isinstance(config, (str, Path)) else config, path
    )
    history_path = root / "activation-history.json"
    history = json.loads(history_path.read_text()) if history_path.is_file() else []
    candidates = [
        item for version in reversed(history[:-1]) for item in items if item.version == version
    ]
    candidates = [item for item in candidates if not active or item.version != active.version]
    if not candidates:
        raise RuntimeError("no previous bundle is available for rollback")
    return activate_bundle(config, candidates[0].version, path)

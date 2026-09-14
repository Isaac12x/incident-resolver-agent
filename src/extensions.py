"""Trusted declarative tool extensions.

Only manifests supplied by an operator are eligible.  A manifest pins a source,
version, digest, and entry point; installation is isolated and never guesses a
package from a model supplied name.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import urllib.request
import venv
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .subprocess_json import run_bounded_json


class RegistryError(RuntimeError):
    pass


_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


@dataclass(frozen=True)
class ToolManifest:
    name: str
    version: str
    source: str
    sha256: str
    entrypoint: str
    kind: str = "python"
    capabilities: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolManifest:
        values = {
            key: data.get(key) for key in ("name", "version", "source", "sha256", "entrypoint")
        }
        if any(not isinstance(value, str) or not value.strip() for value in values.values()):
            raise RegistryError("manifest requires name, version, source, sha256, and entrypoint")
        digest = values["sha256"].lower()
        if not _SAFE.fullmatch(values["name"]) or not _SAFE.fullmatch(values["version"]):
            raise RegistryError("manifest name and version must be safe identifiers")
        if not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*", values["entrypoint"]
        ):
            raise RegistryError("manifest entrypoint must be module:function")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise RegistryError("manifest sha256 must be a hexadecimal digest")
        if (
            values["source"].startswith(("http://", "https://")) is False
            and not Path(values["source"]).is_file()
        ):
            raise RegistryError("manifest source must be an existing file or pinned URL")
        return cls(
            **values,
            kind=str(data.get("kind", "python")),
            capabilities=tuple(data.get("capabilities", ())),
        )


class TrustedToolRegistry:
    def __init__(
        self,
        manifests: list[ToolManifest] | None = None,
        *,
        installer: Callable[..., Any] | None = None,
    ) -> None:
        self.manifests = {item.name: item for item in (manifests or [])}
        self.loaded: dict[str, Any] = {}
        self.installer = installer

    @classmethod
    def from_file(cls, path: Path | str) -> TrustedToolRegistry:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        entries = raw.get("tools", raw) if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            raise RegistryError("registry must contain a tools list")
        return cls([ToolManifest.from_dict(item) for item in entries])

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "name": item.name,
                "version": item.version,
                "kind": item.kind,
                "capabilities": list(item.capabilities),
                "loaded": item.name in self.loaded,
            }
            for item in self.manifests.values()
        ]

    def restore(self, target: Path | str) -> None:
        """Restore successfully installed tools from their digest-pinned local records."""
        root = Path(target).resolve()
        for record in root.glob("*/manifest.json"):
            try:
                manifest = ToolManifest.from_dict(json.loads(record.read_text(encoding="utf-8")))
            except (OSError, ValueError, RegistryError):
                continue
            trusted = self.manifests.get(manifest.name)
            if (
                trusted
                and trusted.version == manifest.version
                and trusted.sha256 == manifest.sha256
            ):
                self.loaded[manifest.name] = self._installed_tool(record.parent, trusted)

    def register_local(self, name: str, tool: Any, *, capabilities: tuple[str, ...] = ()) -> None:
        if not name or name in self.loaded:
            raise RegistryError("tool name is blank or already registered")
        self.loaded[name] = tool
        self.manifests.setdefault(
            name, ToolManifest(name, "local", "local", "0" * 64, "local", "local", capabilities)
        )

    def register_mcp(self, name: str, server: Any) -> None:
        if not all(callable(getattr(server, field, None)) for field in ("list_tools", "call_tool")):
            raise RegistryError("MCP extension must provide list_tools and call_tool")
        self.register_local(name, server)

    @staticmethod
    def _source_bytes(source: str, *, maximum: int = 50_000_000) -> bytes:
        if source.startswith(("http://", "https://")):
            with urllib.request.urlopen(source, timeout=10) as response:  # noqa: S310 - operator pinned URL
                payload = response.read(maximum + 1)
        else:
            with Path(source).open("rb") as handle:
                payload = handle.read(maximum + 1)
        if len(payload) > maximum:
            raise RegistryError("extension artifact exceeds size limit")
        return payload

    def install(self, name: str, target: Path | str, *, allow_install: bool = False) -> Path:
        if not allow_install:
            raise PermissionError("dependency installation is disabled")
        manifest = self.manifests.get(name)
        if manifest is None:
            raise RegistryError(f"tool is not in trusted registry: {name}")
        if manifest.kind != "python":
            raise RegistryError(f"unsupported extension kind: {manifest.kind}")
        payload = self._source_bytes(manifest.source)
        if hashlib.sha256(payload).hexdigest() != manifest.sha256:
            raise RegistryError(f"digest mismatch for trusted tool: {name}")
        destination = Path(target).resolve() / f"{manifest.name}-{manifest.version}"
        destination.mkdir(parents=True, exist_ok=True)
        archive = destination / Path(manifest.source).name
        if not archive.name.endswith(".whl"):
            raise RegistryError("python extension source must be a wheel")
        archive.write_bytes(payload)
        venv.create(destination / "venv", with_pip=True, clear=False)
        command = [
            str(destination / "venv" / "bin" / "python"),
            "-m",
            "pip",
            "install",
            "--no-deps",
            str(archive),
        ]
        runner = self.installer or subprocess.run
        result = runner(command, capture_output=True, text=True, check=False, timeout=120)
        if result.returncode != 0:
            raise RegistryError(f"installation failed for {name}")
        (destination / "manifest.json").write_text(
            json.dumps(manifest.__dict__, sort_keys=True), encoding="utf-8"
        )
        self.loaded[name] = self._installed_tool(destination, manifest)
        return destination

    @staticmethod
    def _installed_tool(
        destination: Path, manifest: ToolManifest
    ) -> Callable[[dict[str, Any]], Any]:
        module, function = manifest.entrypoint.split(":", 1)
        interpreter = destination / "venv" / "bin" / "python"

        def invoke(arguments: dict[str, Any]) -> Any:
            script = (
                "import importlib,json,sys; "
                f"fn=getattr(importlib.import_module({module!r}),{function!r}); "
                "value=fn(json.load(sys.stdin)); print(json.dumps({'value':value},default=str))"
            )
            safe_env = {
                key: value
                for key, value in os.environ.items()
                if not any(
                    marker in key.upper()
                    for marker in ("TOKEN", "SECRET", "PASSWORD", "KEY", "CREDENTIAL")
                )
            }
            result = run_bounded_json(
                [str(interpreter), "-I", "-c", script],
                arguments,
                timeout_seconds=30,
                max_bytes=65536,
                env=safe_env,
            )
            if not result["available"]:
                raise RegistryError(
                    f"installed tool execution failed: {result.get('reason', 'nonzero exit')}"
                )
            return result["result"]["value"]

        return invoke

    def load(self, name: str, target: Path | str | None = None) -> Any:
        manifest = self.manifests.get(name)
        if manifest is None:
            raise RegistryError(f"tool is not in trusted registry: {name}")
        if name not in self.loaded:
            raise RegistryError(f"tool is not installed: {name}")
        return self.loaded[name]

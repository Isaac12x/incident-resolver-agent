"""Durable verification evidence using code-review-graph identities and SHA-256 hashes."""

from __future__ import annotations

import hashlib
import json
import shlex
import sqlite3
import subprocess
from pathlib import Path
from typing import Any


class VerificationGraph:
    def __init__(self, worktree: Path, artifact: Path) -> None:
        self.root = worktree.resolve()
        self.artifact = artifact
        self.data: dict[str, Any] = (
            json.loads(artifact.read_text())
            if artifact.exists()
            else {"version": 1, "nodes": [], "edges": [], "runs": [], "seeds": []}
        )

    def save(self) -> None:
        self.artifact.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.artifact.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, indent=2) + "\n")
        temporary.replace(self.artifact)

    def relative(self, path: str) -> str:
        resolved = (self.root / path).resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("verification path escapes the worktree")
        relative = resolved.relative_to(self.root)
        if any(
            part in {".git", ".agent", "harness-out", ".code-review-graph"}
            for part in relative.parts
        ):
            raise ValueError("verification path is a harness control path")
        return relative.as_posix()

    def files(self) -> dict[str, str]:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        hashes = {}
        for name in sorted(set(result.stdout.decode().split("\0")) - {""}):
            if set(Path(name).parts) & {".agent", "harness-out", ".code-review-graph"}:
                continue
            path = self.root / name
            # Hash links themselves; never read a target outside the checkout.
            if path.is_symlink():
                value = str(path.readlink()).encode()
            elif path.is_file():
                value = path.read_bytes()
            else:
                hashes[name] = "deleted"
                continue
            hashes[name] = hashlib.sha256(value).hexdigest()
        # Keep tombstones for previously verified paths after git records a
        # deletion/rename, so they can be reverified instead of breaking resume.
        for run in self.data["runs"]:
            for path in run["paths"]:
                hashes.setdefault(path, "deleted")
        return hashes

    def graph(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        database = self.root / ".code-review-graph" / "graph.db"
        if not database.exists():
            return [], []
        with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            nodes = [
                dict(row)
                for row in connection.execute(
                    "SELECT kind, name, qualified_name, file_path, file_hash FROM nodes"
                )
            ]
            edges = [
                dict(row)
                for row in connection.execute(
                    "SELECT kind, source_qualified, target_qualified, file_path FROM edges"
                )
            ]
        identities = {}
        for node in nodes:
            name = self.relative(node["file_path"])
            suffix = node["qualified_name"].removeprefix(node["file_path"])
            identities[node["qualified_name"]] = name + suffix
            node.update(file_path=name, qualified_name=name + suffix)
        normalized = []
        for edge in edges:
            if edge["source_qualified"] in identities and edge["target_qualified"] in identities:
                edge.update(
                    source_qualified=identities[edge["source_qualified"]],
                    target_qualified=identities[edge["target_qualified"]],
                    file_path=self.relative(edge["file_path"]),
                )
                normalized.append(edge)
        return nodes, normalized

    @staticmethod
    def adjacency(nodes: list[dict], edges: list[dict]) -> dict[str, set[str]]:
        names = {node["qualified_name"]: node["file_path"] for node in nodes}
        links: dict[str, set[str]] = {path: set() for path in names.values()}
        for edge in edges:
            source, target = names[edge["source_qualified"]], names[edge["target_qualified"]]
            links[source].add(target)
            links[target].add(source)
        return links

    def snapshot(self, paths: list[str] | None, command: str = "") -> dict[str, str]:
        files = self.files()
        if not paths:
            return files
        selected = {self.relative(path) for path in paths}
        if not selected <= files.keys():
            raise ValueError("tested paths must identify repository files")
        for argument in shlex.split(command):
            target = argument.split("=", 1)[-1].split("::", 1)[0].removeprefix("./")
            selected.update(
                path for path in files if path == target or path.startswith(target + "/")
            )
        selected.update(path for path in files if Path(path).name == "conftest.py")
        nodes, edges = self.graph()
        indexed = {node["file_path"]: node["file_hash"] for node in nodes}
        # An absent/stale graph cannot prove dependency completeness. Fall back
        # to all inputs until the next graph refresh, including new files.
        if not indexed or any(files.get(path) != value for path, value in indexed.items()):
            return files
        links = self.adjacency(nodes, edges)
        pending = list(selected)
        while pending:
            for neighbor in links.get(pending.pop(), set()) - selected:
                selected.add(neighbor)
                pending.append(neighbor)
        # Non-indexed inputs include fixtures, lockfiles, configuration and new
        # source files. They always invalidate cached commands when they change.
        selected.update(files.keys() - indexed.keys())
        return {path: files[path] for path in sorted(selected)}

    def cached(self, command: str, paths: list[str] | None, inputs: dict) -> dict | None:
        for run in reversed(self.data["runs"]):
            if run["command"] == command and run["paths"] == sorted(paths or []):
                # A later failed run invalidates an older pass, even at the same hash.
                return run if run["passed"] and run["inputs"] == inputs else None
        return None

    def record(self, command: str, paths: list[str] | None, inputs: dict, result: dict) -> None:
        nodes, edges = self.graph()
        self.data["nodes"] = nodes or [
            {
                "kind": "File",
                "name": path,
                "qualified_name": path,
                "file_path": path,
                "file_hash": value,
            }
            for path, value in inputs.items()
        ]
        for node in self.data["nodes"]:
            if node["file_path"] in inputs:
                node["file_hash"] = inputs[node["file_path"]]
        self.data["edges"] = edges
        self.data["runs"].append(
            {
                "command": command,
                "paths": sorted(paths or []),
                "inputs": inputs,
                **result,
            }
        )
        self.save()

    def pending_checks(self) -> list[str]:
        """Every executed check must have a current pass before publication."""
        latest = {(run["command"], tuple(run["paths"])): run for run in self.data["runs"]}
        return [
            command
            for (command, paths), run in latest.items()
            if not run["passed"] or run["inputs"] != self.snapshot(list(paths), command)
        ]

    def plan(self, seeds: list[str], responsibility: list[str]) -> dict:
        files = self.files()
        boundaries = [self.relative(path) for path in responsibility]
        allowed = {
            path
            for path in files
            if any(
                boundary == "." or path == boundary or path.startswith(boundary + "/")
                for boundary in boundaries
            )
        }
        seeds = sorted({self.relative(path) for path in seeds} or set(self.data["seeds"]))
        if not seeds or not set(seeds) <= allowed:
            raise ValueError("incident seed files must be within the responsibility area")
        if not set(self.data["seeds"]) <= set(seeds):
            raise ValueError("incident seeds are fixed; new repair files may be added")
        self.data["seeds"] = seeds
        nodes, edges = self.graph()
        links = self.adjacency(nodes, edges)
        rings, seen, frontier = [], set(seeds), set(seeds)
        while frontier:
            rings.append(sorted(frontier))
            frontier = set().union(*(links.get(path, set()) for path in frontier)) & allowed - seen
            seen.update(frontier)
        # Graphs miss dynamic relationships: finish with other files in the area.
        if allowed - seen:
            rings.append(sorted(allowed - seen))
        covered = set()
        for run in self.data["runs"]:
            paths = run["paths"]
            if not paths:
                continue  # An unscoped command is not evidence for every graph node.
            current = self.snapshot(paths, run["command"])
            if self.cached(run["command"], paths, current):
                covered.update(paths)
        pending = [sorted(set(ring) - covered) for ring in rings]
        next_ring = next((index for index, paths in enumerate(pending) if paths), None)
        self.save()
        return {
            "phase": "complete"
            if next_ring is None
            else ("fix_incident" if next_ring == 0 else "expand"),
            "rings": rings,
            "pending": pending,
            "next_ring": next_ring,
            "next_paths": pending[next_ring] if next_ring is not None else [],
        }

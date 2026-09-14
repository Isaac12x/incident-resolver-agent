"""Durable filesystem storage and repository worktree management."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from .models import Incident, TaskEvent, TaskRecord, TaskState, new_task_id, utc_now
from .task_catalog import TaskCatalog
from .telemetry import Telemetry
from .tooling import repository_candidates

BUCKETS = ("pending", "active", "waiting", "completed", "blocked", "failed")
STATE_BUCKET = {
    TaskState.RECEIVED: "pending",
    TaskState.WAITING_FOR_DEPLOYMENT: "waiting",
    TaskState.WAITING_FOR_REVIEW: "waiting",
    TaskState.COMPLETED: "completed",
    TaskState.CANCELLED: "completed",
    TaskState.BLOCKED: "blocked",
    TaskState.FAILED: "failed",
}


class RepositoryBusyError(FileExistsError):
    """Another live task is preparing a worktree in this repository."""


class Storage:
    def __init__(self, root: Path | str = ".agent") -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.tasks_root = self.root / "tasks"
        for directory in (
            self.root / "memory" / "repositories",
            self.root / "repositories",
            self.root / "worktrees",
            self.root / "locks",
            self.root / "logs",
            *(self.tasks_root / bucket for bucket in BUCKETS),
        ):
            directory.mkdir(parents=True, exist_ok=True)
        global_memory = self.root / "memory" / "global.md"
        global_memory.touch(exist_ok=True)
        self._initialise_sessions()
        self.catalog = TaskCatalog(self.root / "tasks.sqlite3")
        self._migrate_catalog()
        self.telemetry = Telemetry(self.root)

    def _migrate_catalog(self) -> None:
        """Import legacy snapshots without overwriting committed database state."""
        for bucket in BUCKETS:
            for snapshot in (self.tasks_root / bucket).glob("*/state.json"):
                try:
                    task = TaskRecord.model_validate_json(snapshot.read_text())
                    incident = Incident.model_validate_json(
                        snapshot.with_name("input.json").read_text()
                    )
                    event_file = snapshot.with_name("events.jsonl")
                    events = (
                        [
                            TaskEvent.model_validate_json(line)
                            for line in event_file.read_text().splitlines()
                        ]
                        if event_file.exists()
                        else []
                    )
                    self.catalog.create(task, incident, events)
                except (OSError, ValueError):
                    # A corrupt legacy snapshot cannot become authoritative state.
                    continue

    def _initialise_sessions(self) -> None:
        with closing(sqlite3.connect(self.root / "sessions.sqlite3")) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS messages ("
                "conversation_id TEXT, role TEXT, content TEXT, created_at TEXT)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS observability_events ("
                "event_id TEXT PRIMARY KEY, source TEXT NOT NULL, group_key TEXT NOT NULL, "
                "fingerprint TEXT, payload TEXT NOT NULL, received_at TEXT NOT NULL, "
                "duplicate_of TEXT, task_id TEXT)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_observability_group "
                "ON observability_events(source, group_key)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS incident_history ("
                "task_id TEXT PRIMARY KEY, external_id TEXT NOT NULL, source TEXT NOT NULL, "
                "repository TEXT NOT NULL, environment TEXT NOT NULL, summary TEXT NOT NULL, "
                "description TEXT NOT NULL, root_cause TEXT, outcome TEXT, "
                "created_at TEXT NOT NULL)"
            )
            connection.commit()

    def record_observability_event(
        self, source: str, payload: dict[str, object], *, task_id: str | None = None
    ) -> dict[str, object]:
        """Persist a raw intake event and return stable grouping/deduplication metadata."""
        encoded = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
        alerts = payload.get("alerts") if isinstance(payload, dict) else None
        group_key = str(payload.get("groupKey") or payload.get("group_key") or "")
        if not group_key and isinstance(alerts, list):
            group_key = str(payload.get("receiver") or payload.get("title") or source)
        group_key = group_key or source
        common = payload.get("commonLabels")
        common = common if isinstance(common, dict) else {}
        scopes = set()
        for alert in alerts if isinstance(alerts, list) and alerts else [{}]:
            labels = alert.get("labels", {}) if isinstance(alert, dict) else {}
            labels = {**common, **labels} if isinstance(labels, dict) else common
            repository = labels.get("repository") or labels.get("repo") or payload.get("repository")
            environment = (
                labels.get("environment") or labels.get("env") or payload.get("environment")
            )
            if repository or environment:
                scopes.add((str(repository or "").casefold(), str(environment or "")))
        if scopes:
            scope_hash = hashlib.sha256(json.dumps(sorted(scopes)).encode()).hexdigest()
            group_key = f"{group_key}:{scope_hash}"
        fingerprints = (
            [
                str(item.get("fingerprint"))
                for item in alerts
                if isinstance(item, dict) and item.get("fingerprint")
            ]
            if isinstance(alerts, list)
            else []
        )
        fingerprint = ",".join(sorted(fingerprints)) or str(payload.get("external_id") or "")
        event_id = hashlib.sha256(f"{source}\0{encoded}".encode()).hexdigest()
        received = utc_now().isoformat()
        with closing(sqlite3.connect(self.root / "sessions.sqlite3")) as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT event_id, task_id FROM observability_events WHERE event_id=?", (event_id,)
            ).fetchone()
            if prior:
                return {
                    "event_id": event_id,
                    "group_key": group_key,
                    "fingerprint": fingerprint,
                    "duplicate": True,
                    "duplicate_of": prior[0],
                    "task_id": prior[1],
                }
            grouped = (
                connection.execute(
                    "SELECT event_id, task_id FROM observability_events "
                    "WHERE source=? AND group_key=? "
                    "AND fingerprint=? ORDER BY received_at DESC LIMIT 1",
                    (source, group_key, fingerprint),
                ).fetchone()
                if fingerprint
                else None
            )
            connection.execute(
                "INSERT INTO observability_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    source,
                    group_key,
                    fingerprint,
                    encoded,
                    received,
                    grouped[0] if grouped else None,
                    task_id,
                ),
            )
            connection.commit()
        return {
            "event_id": event_id,
            "group_key": group_key,
            "fingerprint": fingerprint,
            "duplicate": bool(grouped),
            "duplicate_of": grouped[0] if grouped else None,
            "task_id": task_id,
        }

    def list_observability_events(
        self, *, source: str | None = None, group_key: str | None = None, limit: int = 100
    ) -> list[dict[str, object]]:
        if not 1 <= limit <= 500:
            raise ValueError("event limit must be between 1 and 500")
        query = (
            "SELECT event_id, source, group_key, fingerprint, payload, received_at, "
            "duplicate_of, task_id FROM observability_events"
        )
        params: list[str | int] = []
        clauses = []
        if source:
            clauses.append("source=?")
            params.append(source)
        if group_key:
            clauses.append("group_key=?")
            params.append(group_key)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY received_at DESC LIMIT ?"
        params.append(limit)
        with closing(sqlite3.connect(self.root / "sessions.sqlite3")) as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            {
                "event_id": row[0],
                "source": row[1],
                "group_key": row[2],
                "fingerprint": row[3],
                "payload": json.loads(row[4]),
                "received_at": row[5],
                "duplicate_of": row[6],
                "task_id": row[7],
            }
            for row in rows
        ]

    def attach_event_task(self, event_id: str, task_id: str) -> None:
        with closing(sqlite3.connect(self.root / "sessions.sqlite3")) as connection:
            connection.execute(
                "UPDATE observability_events SET task_id=? WHERE event_id=?", (task_id, event_id)
            )
            connection.commit()

    def record_incident_history(
        self,
        task_id: str,
        incident: Incident,
        *,
        root_cause: str | None = None,
        outcome: str | None = None,
    ) -> None:
        with closing(sqlite3.connect(self.root / "sessions.sqlite3")) as connection:
            connection.execute(
                "INSERT INTO incident_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET "
                "root_cause=COALESCE(excluded.root_cause, incident_history.root_cause), "
                "outcome=COALESCE(excluded.outcome, incident_history.outcome)",
                (
                    task_id,
                    incident.external_id,
                    incident.source,
                    incident.repository,
                    incident.environment,
                    incident.summary,
                    incident.description,
                    root_cause,
                    outcome,
                    incident.received_at.isoformat(),
                ),
            )
            connection.commit()

    def incident_history(
        self, *, labeled_only: bool = False, limit: int = 1000
    ) -> list[dict[str, object]]:
        query = (
            "SELECT task_id, external_id, source, repository, environment, summary, "
            "description, root_cause, outcome, created_at FROM incident_history"
        )
        if labeled_only:
            query += " WHERE root_cause IS NOT NULL AND root_cause != ''"
        query += " ORDER BY created_at DESC LIMIT ?"
        with closing(sqlite3.connect(self.root / "sessions.sqlite3")) as connection:
            rows = connection.execute(query, (limit,)).fetchall()
        keys = (
            "task_id",
            "external_id",
            "source",
            "repository",
            "environment",
            "summary",
            "description",
            "root_cause",
            "outcome",
            "created_at",
        )
        return [dict(zip(keys, row, strict=True)) for row in rows]

    @staticmethod
    def _json_write(path: Path, value: object) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".snapshot-", delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, indent=2, default=str, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def create_task(self, incident: Incident) -> TaskRecord:
        existing = self.find_by_incident(
            incident.source, incident.external_id, incident.repository, incident.environment
        )
        if existing:
            return existing
        task_id = new_task_id()
        task = TaskRecord(
            task_id=task_id,
            external_id=incident.external_id,
            source=incident.source,
            conversation_id=f"incident:{task_id}",
            agent_session_id=f"task:{task_id}",
            repository=incident.repository,
            environment=incident.environment,
            summary=incident.summary,
        )
        task, _created = self.catalog.create(task, incident)
        self.task_directory(task.task_id)
        if _created:
            self.telemetry.record("task.created", task_id=task.task_id)
        return task

    def task_directory(self, task_id: str) -> Path:
        if not task_id or "/" in task_id or ".." in task_id:
            raise ValueError("invalid task id")
        task = self.catalog.load(task_id)
        destination = self.tasks_root / STATE_BUCKET.get(task.state, "active") / task_id
        matches = [self.tasks_root / bucket / task_id for bucket in BUCKETS]
        found = [path for path in matches if path.is_dir()]
        if len(found) > 1:
            raise ValueError("multiple artifact directories exist for a registered task")
        if found and found[0] != destination:
            os.replace(found[0], destination)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "artifacts" / "incident").mkdir(parents=True, exist_ok=True)
        if not (destination / "input.json").exists():
            self._json_write(
                destination / "input.json", self.catalog.incident(task_id).model_dump(mode="json")
            )
        if not (destination / "state.json").exists():
            self._json_write(destination / "state.json", task.model_dump(mode="json"))
        event_path = destination / "events.jsonl"
        if not event_path.exists():
            event_path.write_text(
                "".join(event.model_dump_json() + "\n" for event in self.catalog.events(task_id))
            )
        return destination

    def load_task(self, task_id: str) -> TaskRecord:
        return self.catalog.load(task_id)

    def load_incident(self, task_id: str) -> Incident:
        return self.catalog.incident(task_id)

    def save_task(self, task: TaskRecord) -> None:
        task.updated_at = utc_now()
        self.catalog.save(task)
        self._json_write(
            self.task_directory(task.task_id) / "state.json", task.model_dump(mode="json")
        )

    def transition(self, task_id: str, state: TaskState, **updates: object) -> TaskRecord:
        task = self.load_task(task_id)
        task.state = state
        for key, value in updates.items():
            if key not in TaskRecord.model_fields:
                raise ValueError(f"unknown task field: {key}")
            setattr(task, key, value)
        task.updated_at = utc_now()
        self.catalog.save(task, TaskEvent(type=f"task.{state.value}"))
        self.telemetry.record(
            "task.transition",
            task_id=task_id,
            success=state not in {TaskState.FAILED, TaskState.BLOCKED},
        )
        destination = self.task_directory(task_id)
        self._json_write(destination / "state.json", task.model_dump(mode="json"))
        (destination / "events.jsonl").write_text(
            "".join(event.model_dump_json() + "\n" for event in self.catalog.events(task_id))
        )
        return task

    def append_event(self, task_id: str, event: TaskEvent) -> None:
        path = self.task_directory(task_id) / "events.jsonl"
        self.catalog.append_event(task_id, event)
        if event.type == "tool.shell":
            self.telemetry.record(
                "tool.shell",
                task_id=task_id,
                success=event.data.get("returncode", 0) == 0,
                seconds=float(event.data.get("duration_seconds", 0)),
            )
        with path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def events(self, task_id: str) -> list[TaskEvent]:
        return self.catalog.events(task_id)

    def list_tasks(self, *buckets: str) -> list[TaskRecord]:
        selected = buckets or BUCKETS
        for bucket in selected:
            if bucket not in BUCKETS:
                raise ValueError(f"unknown task bucket: {bucket}")
        return [
            task
            for task in self.catalog.tasks()
            if STATE_BUCKET.get(task.state, "active") in selected
        ]

    def find_by_incident(
        self,
        source: str,
        external_id: str,
        repository: str | None = None,
        environment: str | None = None,
    ) -> TaskRecord | None:
        return next(
            (
                task
                for task in self.list_tasks()
                if task.source == source
                and task.external_id == external_id
                and (repository is None or task.repository.casefold() == repository.casefold())
                and (environment is None or task.environment.casefold() == environment.casefold())
            ),
            None,
        )

    def find_by_pr(self, repository: str, number: int) -> TaskRecord | None:
        return next(
            (
                task
                for task in self.list_tasks()
                if task.repository.casefold() == repository.casefold() and task.pr_number == number
            ),
            None,
        )

    def write_artifact(self, task_id: str, relative_path: str, content: str) -> Path:
        base = self.task_directory(task_id).resolve()
        path = (base / relative_path).resolve()
        if not path.is_relative_to(base):
            raise ValueError("artifact path escapes task directory")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def read_memory(self, repository: str | None = None) -> str:
        repository_path = f"repositories/{repository.replace('/', '--')}.md" if repository else None
        path = self.root / "memory" / (repository_path or "global.md")
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def append_memory(self, content: str, repository: str | None = None) -> None:
        repository_path = f"repositories/{repository.replace('/', '--')}.md" if repository else None
        path = self.root / "memory" / (repository_path or "global.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(content.rstrip() + "\n")

    def read_task_memory(self, task_id: str) -> str:
        path = self.task_directory(task_id) / "memory.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def append_task_memory(self, task_id: str, content: str) -> None:
        path = self.task_directory(task_id) / "memory.md"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(content.rstrip() + "\n")

    def add_message(self, conversation_id: str, role: str, content: str) -> None:
        with closing(sqlite3.connect(self.root / "sessions.sqlite3")) as connection:
            connection.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?)",
                (conversation_id, role, content, utc_now().isoformat()),
            )
            connection.commit()

    def messages(self, conversation_id: str) -> list[tuple[str, str]]:
        with closing(sqlite3.connect(self.root / "sessions.sqlite3")) as connection:
            rows = connection.execute(
                "SELECT role, content FROM messages WHERE conversation_id=? ORDER BY rowid",
                (conversation_id,),
            ).fetchall()
        return [(str(role), str(content)) for role, content in rows]

    def search_messages(
        self,
        conversation_id: str,
        pattern: str,
        limit: int = 20,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> list[dict[str, object]]:
        """Search one incident conversation with ripgrep and return bounded matching messages."""
        if not pattern or len(pattern) > 500:
            raise ValueError("conversation search pattern must contain 1-500 characters")
        if not 1 <= limit <= 50:
            raise ValueError("conversation search limit must be between 1 and 50")
        with closing(sqlite3.connect(self.root / "sessions.sqlite3")) as connection:
            rows = connection.execute(
                "SELECT role, content, created_at FROM messages "
                "WHERE conversation_id=? ORDER BY rowid",
                (conversation_id,),
            ).fetchall()
        if not rows:
            return []
        corpus = "".join(
            json.dumps(
                {"role": role, "content": content, "created_at": created_at},
                ensure_ascii=False,
            )
            + "\n"
            for role, content, created_at in rows
        )
        try:
            result = runner(
                ["rg", "--json", "--max-count", str(limit), "--", pattern, "-"],
                input=corpus,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as error:
            raise RuntimeError(f"conversation search could not start ripgrep: {error}") from error
        if result.returncode == 1:
            return []
        if result.returncode != 0:
            raise ValueError(f"invalid conversation search pattern: {result.stderr.strip()}")
        matches: list[dict[str, object]] = []
        for line in result.stdout.splitlines():
            event = json.loads(line)
            if event.get("type") != "match":
                continue
            message = json.loads(event["data"]["lines"]["text"])
            content = str(message["content"])
            matches.append(
                {
                    "role": str(message["role"]),
                    "content": content[:8_000],
                    "created_at": str(message["created_at"]),
                    "truncated": len(content) > 8_000,
                }
            )
        return matches[:limit]

    @contextmanager
    def lock(self, name: str) -> Iterator[None]:
        safe_name = name.replace("/", "--")
        path = self.root / "locks" / f"{safe_name}.lock"
        descriptor: int | None = None
        identity: tuple[int, int] | None = None
        try:
            try:
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                try:
                    stale_stat = path.stat()
                    stale_identity = (stale_stat.st_dev, stale_stat.st_ino)
                except FileNotFoundError:
                    stale_identity = None
                try:
                    owner = int(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    owner = -1
                if owner > 0:
                    try:
                        os.kill(owner, 0)
                    except ProcessLookupError:
                        owner = -1
                    except PermissionError:
                        pass
                if owner > 0:
                    raise RepositoryBusyError(f"lock is held by process {owner}: {name}") from None
                try:
                    current_stat = path.stat()
                    if stale_identity == (current_stat.st_dev, current_stat.st_ino):
                        path.unlink()
                except FileNotFoundError:
                    pass
                try:
                    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    raise RepositoryBusyError(
                        f"repository lock was acquired concurrently: {name}"
                    ) from None
            stat = os.fstat(descriptor)
            identity = (stat.st_dev, stat.st_ino)
            os.write(descriptor, str(os.getpid()).encode())
            yield
        finally:
            if descriptor is not None:
                os.close(descriptor)
                try:
                    stat = path.stat()
                    if identity == (stat.st_dev, stat.st_ino):
                        path.unlink()
                except FileNotFoundError:
                    pass

    @staticmethod
    def _branch_name(external_id: str, task_id: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", external_id).strip(".-_").lower()
        while ".." in slug:
            slug = slug.replace("..", "-")
        if slug.endswith(".lock"):
            slug = slug.removesuffix(".lock")
        slug = slug[:64].rstrip(".-_") or "incident"
        return f"incident-harness/fix/{slug}-{task_id[-6:].lower()}"

    def create_worktree(
        self,
        task: TaskRecord,
        clone_url: str | None = None,
        base_branch: str = "main",
        local_path: Path | str | None = None,
    ) -> Path:
        mirror = self.root / "repositories" / f"{task.repository.replace('/', '--')}.git"
        worktree = self.root / "worktrees" / task.task_id
        with self.lock(task.repository):
            source = self._find_local_repository(task.repository, local_path)
            if source is not None:
                repository = source
                self._refresh_repository(repository, base_branch)
            else:
                if not clone_url:
                    raise RuntimeError(
                        f"no local checkout or clone_url is configured for {task.repository}"
                    )
                repository = mirror
                if not mirror.exists():
                    subprocess.run(["git", "clone", "--mirror", clone_url, str(mirror)], check=True)
                self._refresh_repository(mirror, base_branch)
            branch = task.branch or self._branch_name(task.summary, task.task_id)
            base_ref = self._base_ref(repository, base_branch)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "worktree",
                    "add",
                    "-b",
                    branch,
                    str(worktree),
                    base_ref,
                ],
                check=True,
            )
        task.branch = branch
        self.catalog.register_workspace(task.task_id, worktree)
        self.save_task(task)
        return worktree

    def _find_local_repository(
        self, repository: str, configured_path: Path | str | None = None
    ) -> Path | None:
        candidates: list[Path] = []
        if configured_path:
            configured = Path(configured_path).expanduser()
            if configured.exists():
                return configured.resolve()
        for runtime_root in (self.root, self.root.parent / ".agents"):
            repositories = runtime_root / "repositories"
            candidates.extend(repository_candidates(repositories, repository))
        for candidate in candidates:
            if candidate.exists() and (
                (candidate / "HEAD").exists() or (candidate / ".git").is_dir()
            ):
                return candidate.resolve()
        return None

    def local_repository(self, repository: str) -> Path | None:
        """Find a checkout using the conventional .agent/.agents repository roots."""
        return self._find_local_repository(repository)

    @staticmethod
    def _refresh_repository(repository: Path, base_branch: str) -> None:
        """Fetch or fast-forward a checkout before an incident worktree is created."""
        remote = subprocess.run(
            ["git", "-C", str(repository), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
        )
        if remote.returncode != 0:
            return
        bare = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "--is-bare-repository"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if bare == "true":
            # Keep incident branches out of a mirror's destructive refs/* refresh/prune.
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "config",
                    "--replace-all",
                    "remote.origin.fetch",
                    "+refs/heads/*:refs/remotes/origin/*",
                ],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repository), "config", "remote.origin.mirror", "false"],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "fetch",
                    "--prune",
                    "origin",
                    "+refs/heads/*:refs/remotes/origin/*",
                ],
                check=True,
            )
            return
        status = subprocess.run(
            ["git", "-C", str(repository), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        branch = subprocess.run(
            ["git", "-C", str(repository), "branch", "--show-current"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if not status and branch == base_branch:
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "pull",
                    "--ff-only",
                    "origin",
                    base_branch,
                ],
                check=True,
            )
        else:
            subprocess.run(["git", "-C", str(repository), "fetch", "--prune", "origin"], check=True)

    @staticmethod
    def refresh_worktree(worktree: Path, base_branch: str) -> None:
        """Fetch and merge the latest base branch before resuming a durable session."""
        if not worktree.is_dir():
            return
        remote = subprocess.run(
            ["git", "-C", str(worktree), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
        )
        if remote.returncode != 0:
            return
        subprocess.run(["git", "-C", str(worktree), "fetch", "--prune", "origin"], check=True)
        base_ref = f"origin/{base_branch}"
        verify = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--verify", base_ref],
            capture_output=True,
            check=False,
        )
        if verify.returncode != 0:
            return
        merge = subprocess.run(
            ["git", "-C", str(worktree), "merge", "--no-edit", base_ref],
            capture_output=True,
            text=True,
            check=False,
        )
        if merge.returncode != 0:
            raise RuntimeError(
                f"could not merge origin/{base_branch} into {worktree}: {merge.stderr.strip()}"
            )

    @staticmethod
    def _base_ref(repository: Path, base_branch: str) -> str:
        for ref in (f"origin/{base_branch}", base_branch, "HEAD"):
            result = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "--verify", ref],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                return ref
        raise RuntimeError(f"base branch {base_branch!r} was not found in {repository}")

    def commit_worktree(self, task: TaskRecord, message: str) -> str:
        worktree = self.root / "worktrees" / task.task_id
        if not worktree.is_dir():
            raise FileNotFoundError(worktree)
        subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "add",
                "-A",
                "--",
                ".",
                ":(exclude)harness-out",
                ":(exclude).code-review-graph",
                ":(exclude).code-review-graph.db",
            ],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "-c",
                "user.name=Incident Agent",
                "-c",
                "user.email=incident-agent@localhost",
                "commit",
                "--allow-empty",
                "-m",
                message,
            ],
            check=True,
        )
        result = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def worktree_diff(self, task: TaskRecord) -> str:
        worktree = self.root / "worktrees" / task.task_id
        result = subprocess.run(
            ["git", "-C", str(worktree), "diff", "HEAD^", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout

    def remove_worktree(self, task: TaskRecord) -> None:
        worktree = self.root / "worktrees" / task.task_id
        mirror = self.root / "repositories" / f"{task.repository.replace('/', '--')}.git"
        if mirror.exists() and worktree.exists():
            subprocess.run(
                ["git", "-C", str(mirror), "worktree", "remove", "--force", str(worktree)],
                check=False,
            )
        elif worktree.exists():
            shutil.rmtree(worktree)
        self.catalog.release_workspace(task.task_id)

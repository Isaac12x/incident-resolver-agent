"""Durable filesystem storage and repository worktree management."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from .models import Incident, TaskEvent, TaskRecord, TaskState, utc_now

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


class Storage:
    def __init__(self, root: Path | str = ".agent") -> None:
        self.root = Path(root).resolve()
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

    def _initialise_sessions(self) -> None:
        with closing(sqlite3.connect(self.root / "sessions.sqlite3")) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS messages ("
                "conversation_id TEXT, role TEXT, content TEXT, created_at TEXT)"
            )

    @staticmethod
    def _json_write(path: Path, value: object) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, indent=2, default=str, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        temporary.replace(path)

    def create_task(self, incident: Incident) -> TaskRecord:
        existing = self.find_by_incident(incident.source, incident.external_id)
        if existing:
            return existing
        task = TaskRecord(
            external_id=incident.external_id,
            source=incident.source,
            conversation_id=f"incident:{incident.repository}:{incident.external_id}",
            repository=incident.repository,
            environment=incident.environment,
            summary=incident.summary,
        )
        directory = self.tasks_root / "pending" / task.task_id
        directory.mkdir(parents=True)
        (directory / "artifacts" / "incident").mkdir(parents=True)
        self._json_write(directory / "input.json", incident.model_dump(mode="json"))
        self._json_write(directory / "state.json", task.model_dump(mode="json"))
        self.append_event(task.task_id, TaskEvent(type="task.received"))
        return task

    def task_directory(self, task_id: str) -> Path:
        if not task_id or "/" in task_id or ".." in task_id:
            raise ValueError("invalid task id")
        matches = [self.tasks_root / bucket / task_id for bucket in BUCKETS]
        found = [path for path in matches if path.is_dir()]
        if len(found) != 1:
            raise FileNotFoundError(task_id)
        return found[0]

    def load_task(self, task_id: str) -> TaskRecord:
        path = self.task_directory(task_id) / "state.json"
        return TaskRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def load_incident(self, task_id: str) -> Incident:
        path = self.task_directory(task_id) / "input.json"
        return Incident.model_validate_json(path.read_text(encoding="utf-8"))

    def save_task(self, task: TaskRecord) -> None:
        task.updated_at = utc_now()
        self._json_write(
            self.task_directory(task.task_id) / "state.json", task.model_dump(mode="json")
        )

    def transition(self, task_id: str, state: TaskState, **updates: object) -> TaskRecord:
        source = self.task_directory(task_id)
        task = self.load_task(task_id)
        task.state = state
        for key, value in updates.items():
            if key not in TaskRecord.model_fields:
                raise ValueError(f"unknown task field: {key}")
            setattr(task, key, value)
        task.updated_at = utc_now()
        destination = self.tasks_root / STATE_BUCKET.get(state, "active") / task_id
        if source != destination:
            if destination.exists():
                raise FileExistsError(destination)
            os.replace(source, destination)
        self._json_write(destination / "state.json", task.model_dump(mode="json"))
        self.append_event(task_id, TaskEvent(type=f"task.{state.value}"))
        return task

    def append_event(self, task_id: str, event: TaskEvent) -> None:
        path = self.task_directory(task_id) / "events.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def events(self, task_id: str) -> list[TaskEvent]:
        path = self.task_directory(task_id) / "events.jsonl"
        if not path.exists():
            return []
        return [TaskEvent.model_validate_json(line) for line in path.read_text().splitlines()]

    def list_tasks(self, *buckets: str) -> list[TaskRecord]:
        selected = buckets or BUCKETS
        records: list[TaskRecord] = []
        for bucket in selected:
            if bucket not in BUCKETS:
                raise ValueError(f"unknown task bucket: {bucket}")
            for directory in (self.tasks_root / bucket).iterdir():
                if directory.is_dir():
                    try:
                        records.append(self.load_task(directory.name))
                    except (OSError, ValueError):
                        continue
        return sorted(records, key=lambda task: task.created_at)

    def find_by_incident(self, source: str, external_id: str) -> TaskRecord | None:
        return next(
            (
                task
                for task in self.list_tasks()
                if task.source == source and task.external_id == external_id
            ),
            None,
        )

    def find_by_pr(self, repository: str, number: int) -> TaskRecord | None:
        return next(
            (
                task
                for task in self.list_tasks()
                if task.repository == repository and task.pr_number == number
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
                    raise FileExistsError(f"lock is held by process {owner}: {name}") from None
                try:
                    current_stat = path.stat()
                    if stale_identity == (current_stat.st_dev, current_stat.st_ino):
                        path.unlink()
                except FileNotFoundError:
                    pass
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
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
        return f"agent/{slug}-{task_id[-6:].lower()}"

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
                else:
                    subprocess.run(["git", "-C", str(mirror), "fetch", "--prune"], check=True)
            branch = task.branch or self._branch_name(task.external_id, task.task_id)
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
        self.save_task(task)
        return worktree

    def _find_local_repository(
        self, repository: str, configured_path: Path | str | None = None
    ) -> Path | None:
        candidates: list[Path] = []
        if configured_path:
            candidates.append(Path(configured_path).expanduser())
        for runtime_root in (self.root, self.root.parent / ".agents"):
            repositories = runtime_root / "repositories"
            candidates.extend(
                (
                    repositories / repository.replace("/", "--"),
                    repositories / f"{repository.replace('/', '--')}.git",
                    repositories / repository,
                )
            )
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
            subprocess.run(["git", "-C", str(repository), "fetch", "--prune", "origin"], check=True)
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
                ":(exclude)graphify-out",
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

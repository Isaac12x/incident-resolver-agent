"""Read-only projections of the runtime SQLite database."""

# SQL expressions are deliberately kept readable as adjacent fragments.
# ruff: noqa: E501

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from itertools import chain, groupby
from pathlib import Path
from statistics import mean, median
from typing import Any

_STATES = (
    "active",
    "waiting",
    "waiting_for_deployment",
    "waiting_for_review",
    "blocked",
    "failed",
    "completed",
    "cancelled",
)


def _dt(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo else result.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _safe(text: Any, limit: int = 500) -> str:
    return " ".join(str(text or "").split())[:limit]


def _object(value: Any) -> dict[str, Any]:
    """Decode a JSON value as an object, failing closed for other JSON types."""
    if isinstance(value, dict):
        return value
    if not isinstance(value, (str, bytes, bytearray)):
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, UnicodeDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


class RuntimeReader:
    """Short-lived, query-only snapshots; no runtime constructors are used."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.path = self.root / "runtime.sqlite3"

    def _connect(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA busy_timeout=1000")
        db.execute("BEGIN")
        return db

    def snapshot(
        self, *, page: int = 1, page_size: int = 50, filters: dict[str, str] | None = None
    ) -> dict[str, Any]:
        # Keep both the page arithmetic and SQLite's LIMIT/OFFSET bindings
        # bounded even when an authenticated client supplies an enormous int.
        page = min(1_000_000, max(1, int(page)))
        page_size = max(1, min(100, int(page_size)))
        filters = filters or {}
        try:
            db = self._connect()
        except (OSError, sqlite3.Error) as error:
            return {
                "available": False,
                "error": str(error),
                "tasks": [],
                "summary": {},
                "metrics": [],
            }
        try:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "catalog_tasks" not in tables:
                return {
                    "available": False,
                    "error": "runtime schema has no task catalog",
                    "tasks": [],
                    "summary": {},
                    "metrics": [],
                }
            clauses, args = self._filter_sql(filters)
            where = " AND ".join(clauses) or "1=1"
            source = f" FROM catalog_tasks WHERE {where}"
            total = db.execute("SELECT COUNT(*)" + source, args).fetchone()[0]
            rows = db.execute(
                "SELECT task_id,state,record,incident"
                + source
                + " ORDER BY rowid DESC LIMIT ? OFFSET ?",
                (*args, page_size, (page - 1) * page_size),
            ).fetchall()
            tasks = [self._task(row) for row in rows]
            summary = self._summary(db, tables, where, args)
            metrics = self._metrics(db, tables)
            return {
                "available": True,
                "tasks": tasks,
                "page": page,
                "page_size": page_size,
                "total": total,
                "summary": summary,
                "metrics": metrics,
                "refreshed_at": datetime.now(UTC).isoformat(),
            }
        except (sqlite3.Error, ValueError, TypeError) as error:
            return {
                "available": False,
                "error": f"incompatible runtime schema: {error}",
                "tasks": [],
                "summary": {},
                "metrics": [],
            }
        finally:
            db.close()

    def task(self, task_id: str) -> dict[str, Any] | None:
        try:
            db = self._connect()
            row = db.execute(
                "SELECT task_id,state,record,incident FROM catalog_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            task = self._task(row)
            events = []
            for event_row in db.execute(
                "SELECT sequence,event FROM catalog_events "
                "WHERE task_id=? ORDER BY sequence DESC LIMIT 500",
                (task_id,),
            ):
                try:
                    event = _object(event_row[1])
                    if not event:
                        continue
                    when = _dt(event.get("time"))
                    event_type = str(event.get("type", ""))
                    allowed = event_type.startswith("task.") or event_type in {
                        "tool.shell",
                        "agent.run",
                        "http.request",
                        "review.received",
                    }
                    if not allowed:
                        continue
                    events.append(
                        {
                            "sequence": event_row[0],
                            "type": _safe(event_type, 80),
                            "time": when.isoformat() if when else None,
                            "summary": _safe(self._event_summary(event), 500),
                        }
                    )
                except (TypeError, json.JSONDecodeError, AttributeError):
                    continue
            events.reverse()
            task["events"] = events
            return task
        except (OSError, sqlite3.Error, ValueError):
            return None
        finally:
            if "db" in locals():
                db.close()

    def _task(self, row: sqlite3.Row) -> dict[str, Any]:
        record = _object(row["record"])
        incident = _object(row["incident"])
        # The durable SQL column is authoritative.  JSON is an untrusted display
        # payload and may lag a state transition.
        state = str(row["state"] or "")
        prs: list[dict[str, Any]] = []
        if record.get("pr_number") is not None and record.get("pr_url"):
            prs.append(
                {
                    "repository": _safe(record.get("repository"), 200),
                    "number": record["pr_number"],
                    "url": self._url(record["pr_url"]),
                    "sha": _safe(record.get("pr_head_sha"), 100),
                }
            )
        for item in (
            record.get("pull_requests", []) if isinstance(record.get("pull_requests"), list) else []
        ):
            if isinstance(item, dict) and item.get("number") is not None and item.get("url"):
                prs.append(
                    {
                        "repository": _safe(item.get("repository"), 200),
                        "number": item["number"],
                        "url": self._url(item["url"]),
                        "sha": _safe(item.get("head_sha"), 100),
                    }
                )
        application = record.get("application") or incident.get("application")
        repositories = record.get("repositories") or incident.get("repositories")
        repository_details = self._repositories(repositories)
        primary = record.get("repository") or incident.get("repository")
        primary_fields = {
            field: record.get(field)
            for field in (
                "state",
                "pr_number",
                "pr_url",
                "pr_head_sha",
                "deployment_sha",
                "deployment_url",
                "deployment_environment",
                "playwright_status",
                "verification_sha",
                "verification_status",
            )
            if record.get(field) is not None
        }
        if primary and not any(
            str(item.get("name", "")).casefold() == str(primary).casefold()
            for item in repository_details
        ):
            repository_details.insert(0, {"name": _safe(primary, 200), **primary_fields})
        elif primary and primary_fields:
            for item in repository_details:
                if str(item.get("name", "")).casefold() == str(primary).casefold():
                    for field, value in primary_fields.items():
                        item.setdefault(field, value)
                    break
        for item in repository_details:
            for field in ("pr_url", "deployment_url"):
                if field in item:
                    item[field] = self._url(item[field])
            for field in ("pr_head_sha", "deployment_sha", "verification_sha"):
                if field in item:
                    item[field] = _safe(item[field], 200)
            if "playwright_status" in item:
                item["playwright_status"] = _safe(item["playwright_status"], 100)
            if "verification_status" in item:
                item["verification_status"] = _safe(item["verification_status"], 100)
            if item.get("pr_number") is not None and item.get("pr_url"):
                prs.append(
                    {
                        "repository": _safe(item.get("name"), 200),
                        "number": item["pr_number"],
                        "url": self._url(item.get("pr_url")),
                        "sha": _safe(item.get("pr_head_sha"), 100),
                    }
                )
        repository_text = ", ".join(str(item["name"]) for item in repository_details)
        if not repository_text:
            repository_text = str(primary or "")
        return {
            "id": _safe(row["task_id"], 100),
            "summary": _safe(record.get("summary") or incident.get("summary")),
            "application": _safe(application, 100),
            "repository": _safe(repository_text, 200),
            "environment": _safe(record.get("environment") or incident.get("environment"), 100),
            "state": state,
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
            "age_seconds": self._age(record.get("created_at")),
            "pull_requests": self._unique_prs(prs),
            "repositories": repository_details,
            "repository_details": repository_details,
        }

    @staticmethod
    def _repositories(value: Any) -> list[dict[str, Any]]:
        """Normalize repository membership and repository-level status fields."""
        values: list[tuple[str, Any]] = []
        if isinstance(value, dict):
            values = [(str(name), details) for name, details in value.items()]
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    values.append((str(item.get("name") or item.get("repository") or ""), item))
                else:
                    values.append((str(item), {}))
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for name, details in values:
            name = _safe(name, 200)
            if not name or name.casefold() in seen:
                continue
            seen.add(name.casefold())
            details = details if isinstance(details, dict) else {}
            item: dict[str, Any] = {"name": name}
            for field in (
                "state",
                "changed",
                "merged",
                "pr_number",
                "pr_url",
                "pr_head_sha",
                "deployment_sha",
                "deployment_url",
                "deployment_environment",
                "playwright_status",
                "verification_sha",
                "verification_status",
            ):
                if field in details:
                    item[field] = details[field]
            for field in ("pr_url", "deployment_url"):
                if field in item:
                    item[field] = RuntimeReader._url(item[field])
            for field in (
                "pr_head_sha",
                "deployment_sha",
                "verification_sha",
            ):
                if field in item:
                    item[field] = _safe(item[field], 200)
            if "playwright_status" in item:
                item["playwright_status"] = _safe(item["playwright_status"], 100)
            if "state" in item:
                item["state"] = _safe(item["state"], 100)
            for field in ("changed", "merged"):
                if field in item:
                    item[field] = bool(item[field])
            result.append(item)
        return result

    @staticmethod
    def _url(value: Any) -> str | None:
        value = str(value or "")
        return value if value.startswith(("https://", "http://")) else None

    @staticmethod
    def _unique_prs(prs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[tuple[str, Any]] = set()
        result = []
        for pr in prs:
            key = (str(pr.get("repository") or "").casefold(), pr.get("number"))
            if key not in seen and pr.get("url"):
                seen.add(key)
                result.append(pr)
        return result

    @staticmethod
    def _age(value: Any) -> float | None:
        date = _dt(value)
        return max(0, (datetime.now(UTC) - date).total_seconds()) if date else None

    @staticmethod
    def _filter_sql(filters: dict[str, str]) -> tuple[list[str], list[Any]]:
        clauses, args = [], []
        scalar_expressions = {
            "application": "COALESCE(CASE WHEN json_valid(record) THEN json_extract(record,'$.application') END,"
            "CASE WHEN json_valid(incident) THEN json_extract(incident,'$.application') END)",
            "environment": "COALESCE(CASE WHEN json_valid(record) THEN json_extract(record,'$.environment') END,"
            "CASE WHEN json_valid(incident) THEN json_extract(incident,'$.environment') END)",
        }
        for key, expression in scalar_expressions.items():
            if filters.get(key):
                clauses.append(f"lower({expression}) LIKE lower(?)")
                args.append(f"%{filters[key]}%")
        if filters.get("repository"):
            value = f"%{filters['repository']}%"
            clauses.append(
                "(lower(COALESCE(CASE WHEN json_valid(record) THEN json_extract(record,'$.repository') END,"
                "CASE WHEN json_valid(incident) THEN json_extract(incident,'$.repository') END)) LIKE lower(?)"
                " OR EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(record) AND "
                "json_type(record,'$.repositories') IN ('array','object') THEN json_extract(record,'$.repositories') ELSE '[]' END) member "
                "WHERE lower(CASE WHEN json_type(CASE WHEN json_valid(record) THEN json_extract(record,'$.repositories') END)='object' "
                "THEN member.key ELSE CASE WHEN member.type='object' THEN json_extract(member.value,'$.name') ELSE member.value END END) LIKE lower(?))"
                " OR EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(incident) AND "
                "json_type(incident,'$.repositories') IN ('array','object') THEN json_extract(incident,'$.repositories') ELSE '[]' END) member2 "
                "WHERE lower(CASE WHEN json_type(CASE WHEN json_valid(incident) THEN json_extract(incident,'$.repositories') END)='object' "
                "THEN member2.key ELSE CASE WHEN member2.type='object' THEN json_extract(member2.value,'$.name') ELSE member2.value END END) LIKE lower(?)))"
            )
            args.extend([value, value, value])
        if filters.get("state"):
            state = filters["state"]
            active_states = (
                "received",
                "collecting_context",
                "investigating",
                "reproducing",
                "implementing",
                "testing_local",
                "publishing_pr",
                "testing_deployment",
            )
            if state == "active":
                clauses.append("state IN (" + ",".join("?" for _ in active_states) + ")")
                args.extend(active_states)
            elif state == "waiting":
                clauses.append("state IN ('waiting_for_pr_deployment','waiting_for_review')")
            else:
                clauses.append("state=?")
                args.append(state)
        for key, operator in (("since", ">="), ("until", "<=")):
            if filters.get(key):
                value = filters[key]
                if key == "until" and len(value) == 10:
                    clauses.append(
                        "julianday(CASE WHEN json_valid(record) THEN json_extract(record,'$.created_at') END) "
                        "< julianday(?, '+1 day')"
                    )
                else:
                    clauses.append(
                        f"julianday(CASE WHEN json_valid(record) THEN json_extract(record,'$.created_at') END) {operator} julianday(?)"
                    )
                args.append(value)
        return clauses, args

    def _summary(
        self, db: sqlite3.Connection, tables: set[str], where: str, args: list[str]
    ) -> dict[str, Any]:
        counts = {key: 0 for key in _STATES}
        for row in db.execute(
            f"SELECT state,COUNT(*) n FROM catalog_tasks WHERE {where} GROUP BY state", args
        ):
            state = str(row["state"] or "")
            count = row["n"]
            bucket = self._state_bucket(state)
            counts[bucket] += count
            if bucket in {"waiting_for_deployment", "waiting_for_review"}:
                counts["waiting"] += count
        completed_at: list[float] = []
        timing_missing = timing_invalid = timing_negative = 0
        if "catalog_events" in tables:
            query = (
                f"SELECT t.task_id, t.record, e.event FROM catalog_tasks t "
                "LEFT JOIN catalog_events e ON e.task_id=t.task_id "
                "AND json_valid(e.event)=1 AND json_extract(e.event,'$.type')='task.completed' "
                f"WHERE {where} AND t.state='completed' ORDER BY t.task_id, e.sequence"
            )
            rows = db.execute(query, args)
            for _task_id, task_rows in groupby(rows, key=lambda row: row["task_id"]):
                first = next(task_rows)
                started = _dt(_object(first["record"]).get("created_at"))
                if not started:
                    timing_missing += 1
                    for _ in task_rows:
                        pass
                    continue
                found = False
                invalid = False
                saw_event = False
                for row in chain((first,), task_rows):
                    raw_event = row["event"]
                    if raw_event is None:
                        continue
                    saw_event = True
                    if found:
                        continue
                    event = _object(raw_event)
                    done = _dt(event.get("time"))
                    if not done:
                        invalid = True
                        continue
                    if done < started:
                        timing_negative += 1
                        invalid = True
                        continue
                    completed_at.append((done - started).total_seconds())
                    found = True
                if not saw_event:
                    timing_missing += 1
                elif not found and invalid:
                    timing_invalid += 1
        else:
            timing_missing = counts["completed"]
        # PRs are projected through the same task normalizer as the detail view,
        # so compatibility fields, repository maps, and pull_requests form one
        # case-insensitive union rather than independent maxima.
        pr_keys: set[tuple[str, str]] = set()
        for row in db.execute(
            f"SELECT task_id,state,record,incident FROM catalog_tasks WHERE {where}", args
        ):
            for pr in self._task(row)["pull_requests"]:
                if pr.get("url") and pr.get("number") is not None:
                    pr_keys.add((str(pr.get("repository", "")).casefold(), str(pr["number"])))
        completed, failed = counts["completed"], counts["failed"]
        return {
            **counts,
            "incidents": sum(
                counts[key]
                for key in ("active", "waiting", "blocked", "failed", "completed", "cancelled")
            ),
            "resolved": counts["completed"],
            "prs_opened": len(pr_keys),
            "successes": completed,
            "success_rate": completed / (completed + failed) if completed + failed else None,
            "resolution_seconds_mean": mean(completed_at) if completed_at else None,
            "resolution_seconds_median": median(completed_at) if completed_at else None,
            "resolution_samples": len(completed_at),
            "resolution_samples_missing": timing_missing,
            "resolution_samples_invalid": timing_invalid,
            "resolution_samples_negative": timing_negative,
        }

    @staticmethod
    def _state_bucket(state: str) -> str:
        if state in _STATES:
            return state
        if state in {"waiting_for_pr_deployment", "waiting_for_deployment"}:
            return "waiting_for_deployment"
        if state in {"waiting_for_pr_review", "waiting_for_review"}:
            return "waiting_for_review"
        if state in {"pending", "running", "received"}:
            return "active"
        return "active"

    @staticmethod
    def _metrics(db: sqlite3.Connection, tables: set[str]) -> list[dict[str, Any]]:
        if "telemetry_metrics" not in tables:
            return []
        return [
            dict(row)
            for row in db.execute(
                "SELECT name,calls,failures,seconds FROM telemetry_metrics ORDER BY name LIMIT 100"
            )
        ]

    @staticmethod
    def _event_summary(event: dict[str, Any]) -> str:
        labels = {
            "task.received": "Task received",
            "task.completed": "Task completed",
            "task.failed": "Task failed",
            "task.blocked": "Task blocked",
            "task.waiting_for_review": "Waiting for review",
            "task.waiting_for_pr_deployment": "Waiting for deployment",
        }
        return labels.get(str(event.get("type")), "Lifecycle event")

import json
import sqlite3
from pathlib import Path

from src.dashboard.data import RuntimeReader, _dt, _object


def fixture_db(tmp_path: Path) -> Path:
    db = sqlite3.connect(tmp_path / "runtime.sqlite3")
    db.executescript(
        "CREATE TABLE catalog_tasks(task_id TEXT,state TEXT,record TEXT,incident TEXT);"
        "CREATE TABLE catalog_events(sequence INTEGER PRIMARY KEY,task_id TEXT,event TEXT);"
        "CREATE TABLE telemetry_metrics(name TEXT,calls INTEGER,failures INTEGER,seconds REAL);"
    )
    record = {
        "task_id": "one",
        "state": "completed",
        "summary": "safe",
        "repository": "acme/app",
        "environment": "prod",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T09:00:00+00:00",
        "pr_number": 3,
        "pr_url": "https://github.com/acme/app/pull/3",
    }
    incident = {
        "summary": "safe",
        "source": "pager",
        "repository": "acme/app",
        "environment": "prod",
    }
    db.execute(
        "INSERT INTO catalog_tasks VALUES(?,?,?,?)",
        ("one", "completed", json.dumps(record), json.dumps(incident)),
    )
    db.execute(
        "INSERT INTO catalog_events VALUES(?,?,?)",
        (
            1,
            "one",
            json.dumps(
                {
                    "type": "task.completed",
                    "time": "2026-01-01T00:20:00+00:00",
                    "data": {"secret": "redact"},
                }
            ),
        ),
    )
    db.commit()
    db.close()
    return tmp_path


def test_snapshot_uses_completion_event_and_read_only(tmp_path):
    root = fixture_db(tmp_path)
    result = RuntimeReader(root).snapshot()
    assert result["summary"]["resolved"] == 1
    assert result["summary"]["resolution_seconds_mean"] == 1200
    assert "secret" not in json.dumps(result)
    assert not (root / "runtime.sqlite3-wal").exists()


def test_snapshot_projects_repository_members_and_authoritative_sql_state(tmp_path):
    root = fixture_db(tmp_path)
    db = sqlite3.connect(root / "runtime.sqlite3")
    record = {
        "state": "failed",
        "summary": "multi repo",
        "application": "billing",
        "repositories": {
            "Acme/API": {
                "state": "completed",
                "changed": True,
                "pr_number": 7,
                "pr_url": "https://github.test/pull/7?x=1#frag",
                "pr_head_sha": "head",
                "deployment_sha": "head",
                "deployment_url": "https://preview.test/?x=1#frag",
                "playwright_status": "passed",
                "verification_status": "passed",
            },
            "acme/Web": {
                "state": "waiting_for_review",
                "merged": False,
                "pr_number": 8,
                "pr_url": "https://github.test/pull/8",
            },
        },
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    db.execute(
        "INSERT INTO catalog_tasks VALUES(?,?,?,?)",
        ("multi", "waiting_for_pr_deployment", json.dumps(record), "[]"),
    )
    db.commit()
    db.close()
    result = RuntimeReader(root).snapshot(filters={"repository": "acme/web"})
    assert result["total"] == 1
    task = result["tasks"][0]
    assert task["state"] == "waiting_for_pr_deployment"
    assert task["repositories"][0]["pr_url"].endswith("?x=1#frag")
    assert task["repositories"][0]["state"] == "completed"
    assert task["repositories"][1]["state"] == "waiting_for_review"
    assert task["repositories"][0]["changed"] is True
    assert result["summary"]["waiting"] == 1
    assert result["summary"]["waiting_for_deployment"] == 1


def test_snapshot_pr_union_is_case_insensitive_and_completion_is_one_sample(tmp_path):
    root = fixture_db(tmp_path)
    db = sqlite3.connect(root / "runtime.sqlite3")
    for number, repository in ((3, "ACME/APP"), (4, "acme/other")):
        record = {
            "summary": "done",
            "repository": repository,
            "pr_number": number,
            "pr_url": f"https://github.test/pull/{number}",
            "pull_requests": [
                {
                    "repository": repository.lower(),
                    "number": number,
                    "url": f"https://github.test/pull/{number}",
                }
            ],
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        db.execute(
            "INSERT INTO catalog_tasks VALUES(?,?,?,?)",
            (f"done-{number}", "completed", json.dumps(record), "{}"),
        )
    db.executemany(
        "INSERT INTO catalog_events VALUES(?,?,?)",
        [
            (
                10,
                "done-3",
                json.dumps({"type": "task.completed", "time": "2026-01-01T00:10:00+00:00"}),
            ),
            (
                11,
                "done-3",
                json.dumps({"type": "task.completed", "time": "2026-01-01T00:20:00+00:00"}),
            ),
            (
                12,
                "done-4",
                json.dumps({"type": "task.completed", "time": "2025-12-31T23:00:00+00:00"}),
            ),
        ],
    )
    db.commit()
    db.close()
    summary = RuntimeReader(root).snapshot()["summary"]
    assert summary["prs_opened"] == 2
    assert summary["resolution_samples"] == 2
    assert summary["resolution_samples_negative"] == 1


def test_snapshot_handles_non_object_json_and_offset_date_filters(tmp_path):
    root = fixture_db(tmp_path)
    db = sqlite3.connect(root / "runtime.sqlite3")
    db.execute(
        "INSERT INTO catalog_tasks VALUES(?,?,?,?)",
        ("bad", "active", "[]", "null"),
    )
    db.execute(
        "INSERT INTO catalog_tasks VALUES(?,?,?,?)",
        (
            "offset",
            "active",
            json.dumps(
                {
                    "summary": "offset",
                    "application": "offset",
                    "created_at": "2026-01-01T01:00:00+01:00",
                }
            ),
            "{}",
        ),
    )
    db.commit()
    db.close()
    result = RuntimeReader(root).snapshot(
        filters={
            "application": "offset",
            "since": "2025-12-31T23:30:00+00:00",
            "until": "2026-01-01T00:30:00Z",
        }
    )
    assert result["available"] is True
    assert result["total"] == 1
    assert result["tasks"][0]["id"] == "offset"


def test_task_returns_recent_timeline_page_and_skips_malformed_events(tmp_path):
    root = fixture_db(tmp_path)
    db = sqlite3.connect(root / "runtime.sqlite3")
    events = [
        (
            i + 2,
            "one",
            json.dumps(
                {"type": "task.received", "time": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}+00:00"}
            ),
        )
        for i in range(600)
    ]
    events.append((1000, "one", "[]"))
    db.executemany("INSERT INTO catalog_events VALUES(?,?,?)", events)
    db.commit()
    db.close()
    events = RuntimeReader(root).task("one")["events"]
    assert len(events) == 499
    assert events[0]["sequence"] == 103
    assert events[-1]["sequence"] == 601


def test_reader_reports_unavailable_schema_and_missing_task(tmp_path):
    assert RuntimeReader(tmp_path).snapshot()["available"] is False
    db = sqlite3.connect(tmp_path / "runtime.sqlite3")
    db.execute("CREATE TABLE unrelated(value TEXT)")
    db.commit()
    db.close()
    reader = RuntimeReader(tmp_path)
    assert reader.snapshot()["error"] == "runtime schema has no task catalog"
    assert reader.task("missing") is None


def test_reader_metrics_and_safe_projection_helpers(tmp_path):
    root = fixture_db(tmp_path)
    db = sqlite3.connect(root / "runtime.sqlite3")
    db.execute("INSERT INTO telemetry_metrics VALUES(?,?,?,?)", ("shell", 4, 1, 2.5))
    db.execute(
        "INSERT INTO catalog_events VALUES(?,?,?)",
        (2, "one", json.dumps({"type": "private.event"})),
    )
    db.commit()
    db.close()
    result = RuntimeReader(root).snapshot()
    assert result["metrics"][0]["name"] == "shell"
    assert RuntimeReader._url("file:///etc/passwd") is None
    assert RuntimeReader._url("https://host/path?q=1#fragment").endswith("#fragment")
    assert RuntimeReader._unique_prs(
        [
            {"repository": "Acme/App", "number": 1, "url": "https://x/1"},
            {"repository": "acme/app", "number": 1, "url": "https://x/1"},
        ]
    ) == [{"repository": "Acme/App", "number": 1, "url": "https://x/1"}]


def test_reader_helper_fail_closed_cases_and_repository_lists(tmp_path):
    assert _dt("not a date") is None
    assert _object(42) == {}
    assert _object(b"not json") == {}
    assert _object("[]") == {}
    details = RuntimeReader._repositories([{"name": "Acme/API"}, "acme/api", ""])
    assert details == [{"name": "Acme/API"}]
    assert _object({"ok": True}) == {"ok": True}
    root = fixture_db(tmp_path)
    db = sqlite3.connect(root / "runtime.sqlite3")
    db.execute("UPDATE catalog_tasks SET record=? WHERE task_id='one'", ("{",))
    db.execute("INSERT INTO catalog_events VALUES(?,?,?)", (2, "one", "{"))
    db.execute(
        "INSERT INTO catalog_events VALUES(?,?,?)", (3, "one", json.dumps({"type": "private"}))
    )
    db.commit()
    db.close()
    reader = RuntimeReader(root)
    assert reader.task("one")["summary"] == "safe"
    assert reader.task("missing") is None


def test_reader_handles_incompatible_tables_and_missing_timing(tmp_path):
    db = sqlite3.connect(tmp_path / "runtime.sqlite3")
    db.execute("CREATE TABLE catalog_tasks(task_id TEXT, state TEXT, record TEXT, incident TEXT)")
    db.execute("INSERT INTO catalog_tasks VALUES(?,?,?,?)", ("done", "completed", "{}", "{}"))
    db.commit()
    db.close()
    result = RuntimeReader(tmp_path).snapshot()
    assert result["summary"]["resolution_samples_missing"] == 1
    assert result["metrics"] == []
    db = sqlite3.connect(tmp_path / "broken.sqlite3")
    db.execute("CREATE TABLE catalog_tasks(value TEXT)")
    db.commit()
    db.close()
    (tmp_path / "broken.sqlite3").replace(tmp_path / "runtime.sqlite3")
    assert RuntimeReader(tmp_path).snapshot()["available"] is False


def test_state_bucket_maps_lifecycle_literals():
    assert RuntimeReader._state_bucket("waiting_for_review") == "waiting_for_review"
    assert RuntimeReader._state_bucket("waiting_for_pr_review") == "waiting_for_review"
    assert RuntimeReader._state_bucket("received") == "active"
    assert RuntimeReader._state_bucket("unexpected") == "active"


def test_active_filter_matches_lifecycle_states(tmp_path):
    root = fixture_db(tmp_path)
    db = sqlite3.connect(root / "runtime.sqlite3")
    db.execute(
        "INSERT INTO catalog_tasks VALUES(?,?,?,?)",
        ("investigating", "investigating", json.dumps({"summary": "active"}), "{}"),
    )
    db.commit()
    db.close()
    result = RuntimeReader(root).snapshot(filters={"state": "active"})
    assert result["total"] == 1
    assert result["tasks"][0]["state"] == "investigating"


def test_repository_string_members_filter_without_json_errors(tmp_path):
    root = fixture_db(tmp_path)
    db = sqlite3.connect(root / "runtime.sqlite3")
    db.execute(
        "INSERT INTO catalog_tasks VALUES(?,?,?,?)",
        (
            "strings",
            "active",
            json.dumps({"summary": "strings", "repositories": ["Acme/API", "Acme/Web"]}),
            "{}",
        ),
    )
    db.commit()
    db.close()
    result = RuntimeReader(root).snapshot(filters={"repository": "acme/api"})
    assert result["available"] is True
    assert result["total"] == 1


def test_until_date_includes_the_entire_selected_day(tmp_path):
    root = fixture_db(tmp_path)
    db = sqlite3.connect(root / "runtime.sqlite3")
    db.execute(
        "INSERT INTO catalog_tasks VALUES(?,?,?,?)",
        (
            "day",
            "active",
            json.dumps({"summary": "day", "created_at": "2026-01-02T12:00:00+00:00"}),
            "{}",
        ),
    )
    db.commit()
    db.close()
    result = RuntimeReader(root).snapshot(filters={"since": "2026-01-02", "until": "2026-01-02"})
    assert result["total"] == 1


def test_invalid_completion_is_skipped_for_later_valid_event(tmp_path):
    root = fixture_db(tmp_path)
    db = sqlite3.connect(root / "runtime.sqlite3")
    record = {"summary": "later", "created_at": "2026-01-01T00:00:00+00:00"}
    db.execute(
        "INSERT INTO catalog_tasks VALUES(?,?,?,?)",
        ("later", "completed", json.dumps(record), "{}"),
    )
    db.executemany(
        "INSERT INTO catalog_events VALUES(?,?,?)",
        [
            (20, "later", json.dumps({"type": "task.completed", "time": "garbage"})),
            (
                21,
                "later",
                json.dumps({"type": "task.completed", "time": "2026-01-01T00:10:00+00:00"}),
            ),
        ],
    )
    db.commit()
    db.close()
    summary = RuntimeReader(root).snapshot()["summary"]
    assert summary["resolution_samples"] == 2
    assert summary["resolution_samples_invalid"] == 0


def test_huge_page_is_bounded_without_sqlite_overflow(tmp_path):
    root = fixture_db(tmp_path)
    result = RuntimeReader(root).snapshot(page=10**100, page_size=100)
    assert result["available"] is True
    assert result["page"] == 1_000_000
    assert result["tasks"] == []

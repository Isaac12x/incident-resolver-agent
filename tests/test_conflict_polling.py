from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.config import Config
from src.models import TaskRecord, TaskState
from src.workflow import WorkflowEngine


def tracked(number: int) -> TaskRecord:
    return TaskRecord(
        external_id=f"poll-{number}",
        source="test",
        conversation_id=f"poll:{number}",
        repository="org/repo",
        environment="production",
        summary="poll",
        state=TaskState.WAITING_FOR_DEPLOYMENT,
        pr_number=number,
        pr_head_sha=f"head-{number}",
    )


def polling_engine(tasks, responses):
    engine = object.__new__(WorkflowEngine)
    engine.config = Config()
    engine.storage = SimpleNamespace(list_tasks=lambda *_: list(tasks))
    engine.github = SimpleNamespace(api=object())
    engine.application = SimpleNamespace()
    engine._is_application = lambda task: False
    engine.handle_github_event = AsyncMock()

    async def get_pull_request(task):
        response = responses[task.pr_number]
        if isinstance(response, BaseException):
            raise response
        return response

    engine.github.get_pull_request = get_pull_request
    return engine


@pytest.mark.asyncio
async def test_poll_only_wakes_explicit_dirty_open_prs():
    tasks = [tracked(number) for number in range(1, 6)]
    responses = {
        1: {"state": "open", "head": {"sha": "head-1"}, "mergeable_state": "dirty"},
        2: {"state": "open", "head": {"sha": "head-2"}, "mergeable_state": "clean"},
        3: {"state": "open", "head": {"sha": "head-3"}, "mergeable_state": "unknown"},
        4: {"state": "closed", "head": {"sha": "head-4"}, "mergeable_state": "dirty"},
        5: {"state": "open", "head": {}, "mergeable_state": "dirty"},
    }
    engine = polling_engine(tasks, responses)

    await engine.poll_pull_request_conflicts()

    engine.handle_github_event.assert_awaited_once()
    assert engine.handle_github_event.await_args.args == (
        "pull_request",
        {
            "action": "synchronize",
            "repository": {"full_name": "org/repo"},
            "pull_request": responses[1],
        },
    )


@pytest.mark.asyncio
async def test_poll_continues_after_provider_timeout():
    tasks = [tracked(1), tracked(2)]
    responses = {
        1: TimeoutError("provider timeout"),
        2: {"state": "open", "head": {"sha": "head-2"}, "mergeable_state": "dirty"},
    }
    engine = polling_engine(tasks, responses)

    await engine.poll_pull_request_conflicts()

    engine.handle_github_event.assert_awaited_once()
    assert engine.handle_github_event.await_args.args[1]["pull_request"]["head"]["sha"] == "head-2"


@pytest.mark.asyncio
async def test_poll_skips_tasks_already_in_recovery():
    task = tracked(1).model_copy(update={"conflict_pending": True})
    engine = polling_engine([task], {1: {"state": "open", "mergeable_state": "dirty"}})

    await engine.poll_pull_request_conflicts()

    engine.handle_github_event.assert_not_awaited()

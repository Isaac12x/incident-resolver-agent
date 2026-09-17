"""Declarative lifecycle topology for incident tasks.

The graph describes routing policy; evidence checks remain in the workflow tools.
"""

from __future__ import annotations

from collections.abc import Mapping

from .models import TaskState


class InvalidLifecycleTransition(ValueError):
    """Raised when a task is routed across an undeclared lifecycle edge."""


# These are the routes exercised by both the durable session and the fallback
# phase runner.  External events supply deployment and merge edges.
TRANSITIONS: Mapping[TaskState, frozenset[TaskState]] = {
    TaskState.RECEIVED: frozenset({TaskState.COLLECTING_CONTEXT}),
    TaskState.COLLECTING_CONTEXT: frozenset({TaskState.INVESTIGATING}),
    TaskState.INVESTIGATING: frozenset({TaskState.REPRODUCING}),
    TaskState.REPRODUCING: frozenset(
        {TaskState.REPRODUCING, TaskState.IMPLEMENTING, TaskState.BLOCKED}
    ),
    TaskState.IMPLEMENTING: frozenset(
        {TaskState.TESTING_LOCAL, TaskState.REPRODUCING, TaskState.BLOCKED}
    ),
    TaskState.TESTING_LOCAL: frozenset(
        {
            TaskState.TESTING_LOCAL,
            TaskState.PUBLISHING_PR,
            TaskState.WAITING_FOR_DEPLOYMENT,
            TaskState.IMPLEMENTING,
            TaskState.REPRODUCING,
        }
    ),
    TaskState.PUBLISHING_PR: frozenset(
        {
            TaskState.PUBLISHING_PR,
            TaskState.WAITING_FOR_DEPLOYMENT,
            TaskState.COMPLETED,
            TaskState.REPRODUCING,
        }
    ),
    TaskState.WAITING_FOR_DEPLOYMENT: frozenset({TaskState.TESTING_DEPLOYMENT}),
    TaskState.TESTING_DEPLOYMENT: frozenset(
        {TaskState.WAITING_FOR_REVIEW, TaskState.REPRODUCING, TaskState.BLOCKED}
    ),
    TaskState.WAITING_FOR_REVIEW: frozenset(
        {TaskState.IMPLEMENTING, TaskState.COMPLETED, TaskState.WAITING_FOR_REVIEW}
    ),
    TaskState.COMPLETED: frozenset(),
    TaskState.BLOCKED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}

# A user can merge an existing PR while verification or review repairs are running.
# The webhook handler requires a matching registered PR before taking these edges.
TRANSITIONS = {
    state: targets | {TaskState.COMPLETED}
    if state
    in {
        TaskState.REPRODUCING,
        TaskState.IMPLEMENTING,
        TaskState.TESTING_LOCAL,
        TaskState.WAITING_FOR_DEPLOYMENT,
        TaskState.TESTING_DEPLOYMENT,
    }
    else targets
    for state, targets in TRANSITIONS.items()
}

# Scheduler-active means a worker can make progress without an external event.
# Waiting states are deliberately omitted even though they are graph nodes.
ACTIVE_STATES = frozenset(
    {
        TaskState.RECEIVED,
        TaskState.COLLECTING_CONTEXT,
        TaskState.INVESTIGATING,
        TaskState.REPRODUCING,
        TaskState.IMPLEMENTING,
        TaskState.TESTING_LOCAL,
        TaskState.PUBLISHING_PR,
        TaskState.TESTING_DEPLOYMENT,
    }
)
TERMINAL_STATES = frozenset(
    {TaskState.COMPLETED, TaskState.BLOCKED, TaskState.FAILED, TaskState.CANCELLED}
)


def validate_topology() -> None:
    """Check that every model state is declared and reachable from intake."""
    states = set(TaskState)
    if set(TRANSITIONS) != states:
        raise InvalidLifecycleTransition("lifecycle graph does not cover every task state")
    if any(target not in states for targets in TRANSITIONS.values() for target in targets):
        raise InvalidLifecycleTransition("lifecycle graph contains an unknown target")
    reachable = {TaskState.RECEIVED}
    while True:
        expanded = reachable | {target for source in reachable for target in TRANSITIONS[source]}
        if expanded == reachable:
            break
        reachable = expanded
    # Failure/cancellation are executor outcomes and therefore intentionally do
    # not require a normal work edge from intake.
    expected = states - {TaskState.FAILED, TaskState.CANCELLED}
    if not expected <= reachable:
        missing = ", ".join(sorted(state.value for state in expected - reachable))
        raise InvalidLifecycleTransition(f"unreachable lifecycle states: {missing}")
    if any(TRANSITIONS[state] for state in TERMINAL_STATES):
        raise InvalidLifecycleTransition("terminal lifecycle state has an outgoing edge")


def validate_transition(source: TaskState | str, target: TaskState | str) -> None:
    """Raise unless ``source -> target`` is a declared lifecycle edge."""
    try:
        source_state = TaskState(source)
        target_state = TaskState(target)
    except ValueError as error:
        raise InvalidLifecycleTransition(f"unknown lifecycle state: {error}") from error
    # These are executor outcomes: any non-terminal operation may be cancelled
    # or fail, including a provider error at an otherwise valid graph node.
    if (
        target_state in {TaskState.BLOCKED, TaskState.CANCELLED, TaskState.FAILED}
        and source_state not in TERMINAL_STATES
    ):
        return
    if target_state not in TRANSITIONS[source_state]:
        raise InvalidLifecycleTransition(
            f"illegal lifecycle transition: {source_state.value} -> {target_state.value}"
        )


validate_topology()

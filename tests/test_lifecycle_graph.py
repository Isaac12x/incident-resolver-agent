import pytest

import src.lifecycle_graph as graph
from src.lifecycle_graph import (
    ACTIVE_STATES,
    TERMINAL_STATES,
    InvalidLifecycleTransition,
    validate_topology,
    validate_transition,
)
from src.models import TaskState


def test_declared_graph_covers_real_routes_and_scheduler_activity():
    validate_topology()
    validate_transition(TaskState.RECEIVED, TaskState.COLLECTING_CONTEXT)
    validate_transition(TaskState.PUBLISHING_PR, TaskState.COMPLETED)
    validate_transition(TaskState.TESTING_DEPLOYMENT, TaskState.REPRODUCING)
    assert TaskState.WAITING_FOR_DEPLOYMENT not in ACTIVE_STATES


def test_all_normal_work_edges_are_declared():
    edges = [
        (TaskState.COLLECTING_CONTEXT, TaskState.INVESTIGATING),
        (TaskState.INVESTIGATING, TaskState.REPRODUCING),
        (TaskState.REPRODUCING, TaskState.REPRODUCING),
        (TaskState.REPRODUCING, TaskState.IMPLEMENTING),
        (TaskState.IMPLEMENTING, TaskState.TESTING_LOCAL),
        (TaskState.IMPLEMENTING, TaskState.REPRODUCING),
        (TaskState.TESTING_LOCAL, TaskState.TESTING_LOCAL),
        (TaskState.TESTING_LOCAL, TaskState.PUBLISHING_PR),
        (TaskState.TESTING_LOCAL, TaskState.WAITING_FOR_DEPLOYMENT),
        (TaskState.PUBLISHING_PR, TaskState.PUBLISHING_PR),
        (TaskState.PUBLISHING_PR, TaskState.WAITING_FOR_DEPLOYMENT),
        (TaskState.WAITING_FOR_DEPLOYMENT, TaskState.TESTING_DEPLOYMENT),
        (TaskState.TESTING_DEPLOYMENT, TaskState.WAITING_FOR_REVIEW),
        (TaskState.TESTING_DEPLOYMENT, TaskState.REPRODUCING),
        (TaskState.WAITING_FOR_REVIEW, TaskState.IMPLEMENTING),
        (TaskState.WAITING_FOR_REVIEW, TaskState.WAITING_FOR_REVIEW),
        (TaskState.WAITING_FOR_REVIEW, TaskState.COMPLETED),
    ]
    for source, target in edges:
        validate_transition(source, target)


def test_unknown_states_and_illegal_edges_are_rejected():
    with pytest.raises(InvalidLifecycleTransition, match="unknown lifecycle state"):
        validate_transition("not-a-state", TaskState.RECEIVED)
    with pytest.raises(InvalidLifecycleTransition, match="illegal lifecycle transition"):
        validate_transition(TaskState.COMPLETED, TaskState.RECEIVED)


def test_topology_rejects_missing_node_unknown_target_and_unreachable_state(monkeypatch):
    original = graph.TRANSITIONS
    try:
        monkeypatch.setattr(graph, "TRANSITIONS", {TaskState.RECEIVED: frozenset()})
        with pytest.raises(InvalidLifecycleTransition, match="does not cover"):
            graph.validate_topology()

        complete = dict(original)
        complete[TaskState.RECEIVED] = frozenset({"unknown"})
        monkeypatch.setattr(graph, "TRANSITIONS", complete)
        with pytest.raises(InvalidLifecycleTransition, match="unknown target"):
            graph.validate_topology()

        disconnected = dict(original)
        disconnected[TaskState.WAITING_FOR_REVIEW] = frozenset()
        disconnected[TaskState.TESTING_DEPLOYMENT] = frozenset(
            {TaskState.REPRODUCING, TaskState.BLOCKED}
        )
        monkeypatch.setattr(graph, "TRANSITIONS", disconnected)
        with pytest.raises(InvalidLifecycleTransition, match="unreachable"):
            graph.validate_topology()

        exits = dict(original)
        exits[TaskState.COMPLETED] = frozenset({TaskState.RECEIVED})
        monkeypatch.setattr(graph, "TRANSITIONS", exits)
        with pytest.raises(InvalidLifecycleTransition, match="outgoing edge"):
            graph.validate_topology()
    finally:
        monkeypatch.setattr(graph, "TRANSITIONS", original)


def test_terminal_and_executor_outcomes_have_explicit_semantics():
    for state in TaskState:
        if state not in TERMINAL_STATES:
            validate_transition(state, TaskState.CANCELLED)
            validate_transition(state, TaskState.FAILED)
    try:
        validate_transition(TaskState.COMPLETED, TaskState.IMPLEMENTING)
    except InvalidLifecycleTransition:
        pass
    else:
        raise AssertionError("terminal task must not re-enter the lifecycle")

from repryntt.agents.task_queue import TaskQueue
from repryntt.agents.task_queue_tools import complete_current_task, set_active_queue


def _typed_task(queue: TaskQueue):
    task = queue.add_task(
        title="Write a gated research note",
        priority=2,
        source="test",
        expected_artifact_type="research_md",
        expected_location="workspace/agents/operator/research/gated_note.md",
        downstream_consumer="operator",
        success_criterion="cites at least three sources",
    )
    queue.start_task(task["id"])
    return task


def test_typed_task_cannot_complete_without_critic_gate(tmp_path):
    queue = TaskQueue(str(tmp_path))
    task = _typed_task(queue)

    completed = queue.complete_task(task["id"], summary="done without gate")

    assert completed is None
    current = queue.get_current()
    assert current["id"] == task["id"]
    assert current["status"] == queue.STATUS_IN_PROGRESS
    assert current["last_completion_block"]["reason"] == "critic_gate_required"


def test_typed_task_completes_with_gate_passed(tmp_path):
    queue = TaskQueue(str(tmp_path))
    task = _typed_task(queue)

    completed = queue.complete_task(task["id"], summary="done", gate_passed=True)

    assert completed is not None
    assert completed["status"] == queue.STATUS_COMPLETED
    assert completed["gate_passed"] is True


def test_untyped_legacy_task_can_still_complete(tmp_path):
    queue = TaskQueue(str(tmp_path))
    task = queue.add_task(title="Legacy housekeeping", priority=2, source="test")
    queue.start_task(task["id"])

    completed = queue.complete_task(task["id"], summary="done")

    assert completed is not None
    assert completed["status"] == queue.STATUS_COMPLETED
    assert completed["gate_passed"] is False


def test_agent_tool_does_not_advance_typed_task_without_gate(tmp_path):
    queue = TaskQueue(str(tmp_path))
    task = _typed_task(queue)
    next_task = queue.add_task(title="Next task", priority=3, source="test")
    set_active_queue(queue)

    result = complete_current_task(summary="done without gate")

    assert "Completion blocked" in result
    assert queue.get_current()["id"] == task["id"]
    assert queue._find_task(next_task["id"])["status"] == queue.STATUS_QUEUED

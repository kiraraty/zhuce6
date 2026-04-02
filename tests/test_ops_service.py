import time

from ops.service import RepeatedTask


def test_repeated_task_snapshot_includes_recent_runs() -> None:
    task = RepeatedTask("demo", lambda: None, interval_seconds=1)
    task.start()
    deadline = time.time() + 2.5
    while time.time() < deadline:
        snapshot = task.snapshot()
        if snapshot["run_count"] >= 1:
            break
        time.sleep(0.05)
    task.stop()

    snapshot = task.snapshot()
    assert snapshot["run_count"] >= 1
    assert len(snapshot["recent_runs"]) >= 1
    assert snapshot["recent_runs"][-1]["status"] == "completed"

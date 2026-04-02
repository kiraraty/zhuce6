from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from core import process_manager


def test_process_manager_stops_pid_file_process(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(process_manager, "PID_DIR", tmp_path)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    pid_file = tmp_path / "zhuce6-worker.pid"
    pid_file.write_text(str(proc.pid), encoding="utf-8")
    try:
        assert process_manager.read_pid("worker") == proc.pid
        assert process_manager.is_running(proc.pid) is True
        assert process_manager.stop_process("worker", timeout=1.0) is True
        proc.wait(timeout=5)
        assert not pid_file.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_process_manager_status_all_reports_pid_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(process_manager, "PID_DIR", tmp_path)
    (tmp_path / "zhuce6-main.pid").write_text("999999", encoding="utf-8")

    statuses = process_manager.status_all()

    assert len(statuses) == 1
    assert statuses[0]["name"] == "main"
    assert statuses[0]["pid"] == 999999
    assert statuses[0]["pid_file"] == str(tmp_path / "zhuce6-main.pid")


def test_process_manager_stop_all_also_stops_orphan_repo_processes(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(process_manager, "PID_DIR", tmp_path)
    (tmp_path / "zhuce6-main.pid").write_text("111", encoding="utf-8")
    stopped: list[tuple[int, str | None]] = []

    monkeypatch.setattr(process_manager, "_list_repo_process_pids", lambda: [222, 333])
    monkeypatch.setattr(
        process_manager,
        "_stop_pid",
        lambda pid, timeout=5.0, remove_name=None: stopped.append((pid, remove_name)) or True,
    )

    result = process_manager.stop_all(timeout=1.0)

    assert result == {"main": True, "orphan_pids": [222, 333]}
    assert stopped == [
        (111, "main"),
        (222, None),
        (333, None),
    ]

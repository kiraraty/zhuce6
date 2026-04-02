"""Cross-platform PID-file based process management for zhuce6."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import signal
import subprocess
import time

from .paths import STATE_DIR


PID_DIR = STATE_DIR
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def pid_file(name: str) -> Path:
    return Path(PID_DIR) / f"zhuce6-{name}.pid"


def write_pid(name: str, pid: int | None = None) -> Path:
    path = pid_file(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(int(pid if pid is not None else os.getpid())), encoding="utf-8")
    return path


def read_pid(name: str) -> int | None:
    path = pid_file(name)
    if not path.is_file():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return None


def is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if process == 0:
            return False
        ctypes.windll.kernel32.CloseHandle(process)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    proc_stat = Path("/proc") / str(pid) / "stat"
    if proc_stat.is_file():
        try:
            fields = proc_stat.read_text(encoding="utf-8").split()
        except OSError:
            return True
        if len(fields) >= 3 and fields[2] == "Z":
            return False
    return True


def remove_pid(name: str) -> None:
    try:
        pid_file(name).unlink()
    except FileNotFoundError:
        return


def _send_terminate(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T"], capture_output=True, text=True, check=False)
        return
    os.kill(pid, signal.SIGTERM)


def _send_kill(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True, check=False)
        return
    os.kill(pid, signal.SIGKILL)


def _stop_pid(pid: int | None, timeout: float = 5.0, *, remove_name: str | None = None) -> bool:
    if pid is None:
        if remove_name:
            remove_pid(remove_name)
        return False
    if pid == os.getpid():
        if remove_name:
            remove_pid(remove_name)
        return True
    if not is_running(pid):
        if remove_name:
            remove_pid(remove_name)
        return False

    try:
        _send_terminate(pid)
    except OSError:
        pass

    deadline = time.time() + max(0.1, timeout)
    while time.time() < deadline:
        if not is_running(pid):
            if remove_name:
                remove_pid(remove_name)
            return True
        time.sleep(0.1)

    try:
        _send_kill(pid)
    except OSError:
        pass

    force_deadline = time.time() + 2.0
    while time.time() < force_deadline:
        if not is_running(pid):
            if remove_name:
                remove_pid(remove_name)
            return True
        time.sleep(0.1)

    if not is_running(pid):
        if remove_name:
            remove_pid(remove_name)
        return True
    return False


def _list_repo_process_pids() -> list[int]:
    if os.name == "nt":
        return []
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return []
    project_root = PROJECT_ROOT.resolve()
    current_pid = os.getpid()
    matched: list[int] = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == current_pid:
            continue
        try:
            cwd = (entry / "cwd").resolve()
        except OSError:
            continue
        if cwd != project_root:
            continue
        try:
            cmdline = (entry / "cmdline").read_text(encoding="utf-8", errors="ignore").replace("\x00", " ")
        except OSError:
            continue
        if "main.py" not in cmdline or "zhuce6" not in cmdline:
            continue
        matched.append(pid)
    return sorted(set(matched))


def stop_process(name: str, timeout: float = 5.0) -> bool:
    return _stop_pid(read_pid(name), timeout=timeout, remove_name=name)


def stop_all(timeout: float = 5.0) -> dict[str, bool]:
    results: dict[str, bool] = {}
    for path in sorted(Path(PID_DIR).glob("zhuce6-*.pid")):
        name = path.stem.removeprefix("zhuce6-")
        results[name] = stop_process(name, timeout=timeout)
    orphan_pids = _list_repo_process_pids()
    for pid in orphan_pids:
        _stop_pid(pid, timeout=timeout)
    results["orphan_pids"] = orphan_pids
    return results


def status_all() -> list[dict[str, object]]:
    statuses: list[dict[str, object]] = []
    for path in sorted(Path(PID_DIR).glob("zhuce6-*.pid")):
        name = path.stem.removeprefix("zhuce6-")
        pid = read_pid(name)
        statuses.append(
            {
                "name": name,
                "pid": pid,
                "running": bool(pid and is_running(pid)),
                "pid_file": str(path),
            }
        )
    return statuses

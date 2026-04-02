"""Background task helpers for the single-process zhuce6 runtime."""

from __future__ import annotations

from collections import deque
from datetime import datetime
import threading
import time
from typing import Callable


def _isoformat_timestamp(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value).isoformat(timespec="seconds")


class RepeatedTask:
    def __init__(self, name: str, fn: Callable[[], None], interval_seconds: int) -> None:
        self.name = name
        self.fn = fn
        self.interval_seconds = max(1, int(interval_seconds))
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._run_count = 0
        self._success_count = 0
        self._failure_count = 0
        self._last_started_at: float | None = None
        self._last_finished_at: float | None = None
        self._last_duration_seconds: float | None = None
        self._last_error: str | None = None
        self._next_run_at: float | None = None
        self._is_running = False
        self._recent_runs: deque[dict[str, object]] = deque(maxlen=20)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        with self._lock:
            self._next_run_at = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"zhuce6-{self.name}")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            cycle_started = time.time()
            cycle_error: str | None = None
            with self._lock:
                self._is_running = True
                self._run_count += 1
                self._last_started_at = cycle_started
                self._last_error = None
            try:
                self.fn()
            except Exception as exc:
                with self._lock:
                    self._failure_count += 1
                    self._last_error = str(exc)
                    cycle_error = str(exc)
                print(f"[zhuce6:{self.name}] background task error: {exc}")
            else:
                with self._lock:
                    self._success_count += 1
                    self._last_error = None
            finally:
                cycle_finished = time.time()
                with self._lock:
                    self._is_running = False
                    self._last_finished_at = cycle_finished
                    self._last_duration_seconds = round(cycle_finished - cycle_started, 3)
                    self._recent_runs.append(
                        {
                            "started_at": _isoformat_timestamp(cycle_started),
                            "finished_at": _isoformat_timestamp(cycle_finished),
                            "duration_seconds": self._last_duration_seconds,
                            "status": "failed" if cycle_error else "completed",
                            "error": cycle_error,
                        }
                    )
            elapsed = time.time() - cycle_started
            wait_seconds = max(0.0, self.interval_seconds - elapsed)
            with self._lock:
                self._next_run_at = time.time() + wait_seconds
            if self._stop_event.wait(wait_seconds):
                break

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            if self._is_running:
                status = "running"
            elif self._run_count == 0:
                status = "pending"
            elif self._last_error:
                status = "degraded"
            else:
                status = "healthy"
            return {
                "name": self.name,
                "status": status,
                "interval_seconds": self.interval_seconds,
                "run_count": self._run_count,
                "success_count": self._success_count,
                "failure_count": self._failure_count,
                "is_running": self._is_running,
                "last_started_at": _isoformat_timestamp(self._last_started_at),
                "last_finished_at": _isoformat_timestamp(self._last_finished_at),
                "last_duration_seconds": self._last_duration_seconds,
                "last_error": self._last_error,
                "next_run_at": _isoformat_timestamp(self._next_run_at),
                "recent_runs": list(self._recent_runs),
            }

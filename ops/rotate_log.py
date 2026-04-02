"""Rotate log parsing helpers for zhuce6."""

from __future__ import annotations

from collections import deque
from datetime import datetime
import re
import sys

from core.paths import DEFAULT_DASHBOARD_LOG_FILE

ROTATE_SUMMARY_PATTERN = re.compile(
    r"^\[(?P<time>[0-9:]+)\] \[rotate\] summary \| 主池: (?P<main_before>\d+) → (?P<main_after>\d+) "
    r"\| 401删除: (?P<deleted_401>\d+)"
    r"(?: \| quota探测: (?P<quota_probed>\d+) \| probe401: (?P<quota_probe_401>\d+) "
    r"\| probe429: (?P<quota_probe_429>\d+) \| probe跳过: (?P<quota_probe_skipped>\d+))?"
    r"(?: \| 429删除: (?P<deleted_429>\d+))?$"
)


def _dashboard_log_path():
    main_module = sys.modules.get("main")
    return getattr(main_module, "DEFAULT_DASHBOARD_LOG_FILE", DEFAULT_DASHBOARD_LOG_FILE)


def _parse_rotate_summary_line(line: str) -> dict[str, object] | None:
    match = ROTATE_SUMMARY_PATTERN.match(str(line or "").strip())
    if not match:
        return None
    payload: dict[str, object] = {"time": match.group("time"), "raw": str(line or "").strip()}
    for key in (
        "main_before",
        "main_after",
        "deleted_401",
        "quota_probed",
        "quota_probe_401",
        "quota_probe_429",
        "quota_probe_skipped",
        "deleted_429",
    ):
        raw_value = match.group(key)
        payload[key] = int(raw_value) if raw_value is not None else 0
    return payload



def _empty_rotate_current_summary() -> dict[str, object]:
    return {
        "time": None,
        "raw": None,
        "main_before": None,
        "main_after": None,
        "deleted_401": 0,
        "quota_probed": 0,
        "quota_probe_401": 0,
        "quota_probe_429": 0,
        "quota_probe_skipped": 0,
        "deleted_429": 0,
        "partial": True,
        "event_count": 0,
    }



def _update_rotate_current_summary(payload: dict[str, object], line: str) -> None:
    stripped = str(line or "").strip()
    if not stripped:
        return
    payload["raw"] = stripped
    payload["event_count"] = int(payload.get("event_count") or 0) + 1

    prefix_match = re.match(r"^\[(?P<time>[0-9:]+)\]", stripped)
    if prefix_match:
        payload["time"] = prefix_match.group("time")

    if " quota probe → " in stripped:
        payload["quota_probed"] = int(payload.get("quota_probed") or 0) + 1
        if "quota probe → 429" in stripped:
            payload["quota_probe_429"] = int(payload.get("quota_probe_429") or 0) + 1
        elif "quota probe → 401 invalidated" in stripped or "quota probe → deactivated" in stripped:
            payload["quota_probe_401"] = int(payload.get("quota_probe_401") or 0) + 1
        return

    if " 401删除" in stripped:
        payload["deleted_401"] = int(payload.get("deleted_401") or 0) + 1
    elif " 429删除" in stripped:
        payload["deleted_429"] = int(payload.get("deleted_429") or 0) + 1

    if payload.get("main_before") is not None:
        payload["main_after"] = int(payload.get("main_before") or 0) - int(payload.get("deleted_401") or 0) - int(payload.get("deleted_429") or 0)



def _rotate_log_tail(limit: int = 120, event_limit: int = 16) -> dict[str, object]:
    log_path = _dashboard_log_path()
    if not log_path.exists():
        return {
            "available": False,
            "path": str(log_path),
            "updated_at": None,
            "updated_at_iso": None,
            "error": "dashboard log file not found",
            "lines": [],
            "recent_events": [],
            "latest_summary": None,
            "current_summary": None,
        }
    try:
        latest_summary = None
        current_summary = _empty_rotate_current_summary()
        current_events_seen = False
        with log_path.open("r", encoding="utf-8", errors="replace") as fh:
            rotate_lines: deque[str] = deque(maxlen=limit)
            for raw_line in fh:
                if "[rotate]" not in raw_line:
                    continue
                line = raw_line.rstrip("\r\n")
                rotate_lines.append(line)
                parsed_summary = _parse_rotate_summary_line(line)
                if parsed_summary is not None:
                    latest_summary = parsed_summary
                    current_summary = _empty_rotate_current_summary()
                    current_events_seen = False
                    continue
                _update_rotate_current_summary(current_summary, line)
                current_events_seen = True
        stat = log_path.stat()
    except OSError as exc:
        return {
            "available": False,
            "path": str(log_path),
            "updated_at": None,
            "updated_at_iso": None,
            "error": str(exc),
            "lines": [],
            "recent_events": [],
            "latest_summary": None,
            "current_summary": None,
        }

    lines = list(rotate_lines)
    recent_events = [line for line in lines if "summary" not in line][-max(1, event_limit):]
    return {
        "available": True,
        "path": str(log_path),
        "updated_at": stat.st_mtime,
        "updated_at_iso": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        "error": None,
        "lines": lines,
        "recent_events": recent_events,
        "latest_summary": latest_summary,
        "current_summary": current_summary if current_events_seen else None,
    }


parse_rotate_summary_line = _parse_rotate_summary_line
empty_rotate_current_summary = _empty_rotate_current_summary
update_rotate_current_summary = _update_rotate_current_summary
rotate_log_tail = _rotate_log_tail

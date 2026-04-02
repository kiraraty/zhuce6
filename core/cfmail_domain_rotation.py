"""cfmail domain blacklist tracking and rotation gating."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
import threading
import time
from typing import Any

BLACKLIST_ERROR_CODES = frozenset({"registration_disallowed", "unsupported_email"})
DEFAULT_ROTATION_WINDOW = 10
DEFAULT_ROTATION_THRESHOLD = 6
DEFAULT_ROTATION_COOLDOWN_SECONDS = 300
DEFAULT_ROTATION_MAX_SUCCESSES = 2


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(str(os.getenv(name, default)).strip() or str(default)))
    except Exception:
        return max(minimum, default)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def extract_email_domain(payload: dict[str, Any] | None) -> str:
    raw = payload if isinstance(payload, dict) else {}
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    domain = str(metadata.get("email_domain") or "").strip().lower()
    if domain:
        return domain
    email = str(raw.get("email") or "").strip().lower()
    if "@" not in email:
        return ""
    return email.rsplit("@", 1)[-1].strip().lower()


@dataclass(frozen=True)
class DomainAttempt:
    domain: str
    stage: str
    success: bool
    proxy_key: str
    error_message: str
    blacklist_code: str = ""
    backend_failure: bool = False
    recorded_at: float = field(default_factory=time.time)

    @property
    def is_blacklist_failure(self) -> bool:
        return bool(self.blacklist_code)


def classify_domain_attempt(payload: dict[str, Any] | None, *, proxy_key: str = "") -> DomainAttempt | None:
    raw = payload if isinstance(payload, dict) else {}
    domain = extract_email_domain(raw)
    if not domain:
        return None
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    stage = str(raw.get("stage") or "").strip()
    success = bool(raw.get("success"))
    error_message = str(raw.get("error_message") or "").strip()
    blacklist_code = ""
    if stage == "create_account":
        candidate = str(metadata.get("create_account_error_code") or "").strip().lower()
        if candidate in BLACKLIST_ERROR_CODES:
            blacklist_code = candidate
    backend_failure = stage == "mailbox"
    return DomainAttempt(
        domain=domain,
        stage=stage,
        success=success,
        proxy_key=str(proxy_key or "").strip(),
        error_message=error_message,
        blacklist_code=blacklist_code,
        backend_failure=backend_failure,
    )


@dataclass
class RotationDecision:
    should_rotate: bool
    domain: str = ""
    reason: str = ""
    blacklist_failures: int = 0
    successes: int = 0
    window_size: int = 0


class DomainHealthTracker:
    def __init__(
        self,
        *,
        window_size: int | None = None,
        blacklist_threshold: int | None = None,
        rotation_cooldown_seconds: int | None = None,
        max_successes_in_window: int | None = None,
    ) -> None:
        self.window_size = window_size or _env_int(
            "ZHUCE6_CFMAIL_ROTATION_WINDOW",
            DEFAULT_ROTATION_WINDOW,
        )
        self.blacklist_threshold = blacklist_threshold or _env_int(
            "ZHUCE6_CFMAIL_ROTATION_BLACKLIST_THRESHOLD",
            DEFAULT_ROTATION_THRESHOLD,
        )
        self.rotation_cooldown_seconds = rotation_cooldown_seconds or _env_int(
            "ZHUCE6_CFMAIL_ROTATION_COOLDOWN_SECONDS",
            DEFAULT_ROTATION_COOLDOWN_SECONDS,
        )
        self.max_successes_in_window = max_successes_in_window or _env_int(
            "ZHUCE6_CFMAIL_ROTATION_MAX_SUCCESSES",
            DEFAULT_ROTATION_MAX_SUCCESSES,
        )
        self._lock = threading.RLock()
        self._events: dict[str, deque[DomainAttempt]] = {}
        self._rotation_state: dict[str, Any] = {
            "in_progress": False,
            "active_domain": "",
            "last_blacklisted_domain": "",
            "last_new_domain": "",
            "last_reason": "",
            "last_error": "",
            "last_rotated_at": "",
            "last_checked_at": "",
            "cooldown_until": 0.0,
        }

    def record(self, attempt: DomainAttempt) -> RotationDecision:
        with self._lock:
            events = self._events.setdefault(attempt.domain, deque(maxlen=self.window_size))
            events.append(attempt)
            self._rotation_state["active_domain"] = attempt.domain
            self._rotation_state["last_checked_at"] = _utc_now()
            return self._evaluate_locked(attempt.domain)

    def _evaluate_locked(self, domain: str) -> RotationDecision:
        events = list(self._events.get(domain) or [])
        if not events:
            return RotationDecision(should_rotate=False, domain=domain)
        blacklist_failures = sum(1 for item in events if item.is_blacklist_failure)
        successes = sum(1 for item in events if item.success)
        backend_failures = sum(1 for item in events if item.backend_failure)
        if time.time() < float(self._rotation_state.get("cooldown_until") or 0):
            return RotationDecision(
                should_rotate=False,
                domain=domain,
                reason="rotation cooldown active",
                blacklist_failures=blacklist_failures,
                successes=successes,
                window_size=len(events),
            )
        if backend_failures > 0 and blacklist_failures == 0:
            return RotationDecision(
                should_rotate=False,
                domain=domain,
                reason="backend failure detected",
                blacklist_failures=blacklist_failures,
                successes=successes,
                window_size=len(events),
            )
        if len(events) < self.window_size:
            return RotationDecision(
                should_rotate=False,
                domain=domain,
                reason="insufficient signal window",
                blacklist_failures=blacklist_failures,
                successes=successes,
                window_size=len(events),
            )
        if blacklist_failures >= self.blacklist_threshold and successes <= self.max_successes_in_window:
            return RotationDecision(
                should_rotate=True,
                domain=domain,
                reason="blacklist threshold reached",
                blacklist_failures=blacklist_failures,
                successes=successes,
                window_size=len(events),
            )
        return RotationDecision(
            should_rotate=False,
            domain=domain,
            reason="threshold not met",
            blacklist_failures=blacklist_failures,
            successes=successes,
            window_size=len(events),
        )

    def mark_rotation_started(self, domain: str, reason: str) -> None:
        with self._lock:
            self._rotation_state["in_progress"] = True
            self._rotation_state["last_blacklisted_domain"] = domain
            self._rotation_state["last_reason"] = reason
            self._rotation_state["last_error"] = ""

    def mark_rotation_completed(self, old_domain: str, new_domain: str) -> None:
        with self._lock:
            self._rotation_state["in_progress"] = False
            self._rotation_state["active_domain"] = new_domain
            self._rotation_state["last_blacklisted_domain"] = old_domain
            self._rotation_state["last_new_domain"] = new_domain
            self._rotation_state["last_error"] = ""
            self._rotation_state["last_rotated_at"] = _utc_now()
            self._rotation_state["cooldown_until"] = time.time() + self.rotation_cooldown_seconds
            self._events.pop(old_domain, None)

    def mark_rotation_failed(self, domain: str, error: str) -> None:
        with self._lock:
            self._rotation_state["in_progress"] = False
            self._rotation_state["last_blacklisted_domain"] = domain
            self._rotation_state["last_error"] = str(error or "").strip()[:300]
            self._rotation_state["cooldown_until"] = time.time() + self.rotation_cooldown_seconds

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "in_progress": bool(self._rotation_state.get("in_progress")),
                "active_domain": str(self._rotation_state.get("active_domain") or ""),
                "last_blacklisted_domain": str(self._rotation_state.get("last_blacklisted_domain") or ""),
                "last_new_domain": str(self._rotation_state.get("last_new_domain") or ""),
                "last_reason": str(self._rotation_state.get("last_reason") or ""),
                "last_error": str(self._rotation_state.get("last_error") or ""),
                "last_rotated_at": str(self._rotation_state.get("last_rotated_at") or ""),
                "last_checked_at": str(self._rotation_state.get("last_checked_at") or ""),
                "window_size": self.window_size,
                "blacklist_threshold": self.blacklist_threshold,
                "max_successes_in_window": self.max_successes_in_window,
            }

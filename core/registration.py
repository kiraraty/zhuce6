"""Registration runtime loops for zhuce6."""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import random
import sys
import threading
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from core.registry import load_all
from core.settings import AppSettings
from dashboard.api import _count_cpa_files, _fetch_management_auth_files, _is_regular_free_account
from ops.common import create_backend_client, get_management_key
from ops.rotate_runtime import _maybe_reconcile_cpa_runtime
from platforms.chatgpt.pool import now_iso, update_token_record
from core.chatgpt_flow_runner import run_chatgpt_register_once

DEFAULT_ADD_PHONE_STOPLOSS_WINDOW = 12
DEFAULT_ADD_PHONE_STOPLOSS_THRESHOLD = 8
DEFAULT_ADD_PHONE_STOPLOSS_COOLDOWN_SECONDS = 300
DEFAULT_ADD_PHONE_STOPLOSS_MAX_SUCCESSES = 2
DEFAULT_WAIT_OTP_STOPLOSS_WINDOW = 6
DEFAULT_WAIT_OTP_STOPLOSS_THRESHOLD = 4
DEFAULT_WAIT_OTP_STOPLOSS_COOLDOWN_SECONDS = 300
DEFAULT_WAIT_OTP_LIVE_ABORT_THRESHOLD = 0
DEFAULT_WAIT_OTP_LIVE_ABORT_AGE_SECONDS = 90
DEFAULT_CFMAIL_FRESH_DOMAIN_ATTEMPT_BUDGET = 2

def _classify_token_file(*args, **kwargs):  # type: ignore[no-untyped-def]
    from ops.scan import classify_token_file

    return classify_token_file(*args, **kwargs)


def _compat_main_attr(name: str, default: object) -> object:
    main_module = sys.modules.get("main")
    if main_module is None:
        return default
    return getattr(main_module, name, default)

class RegistrationLoop:
    """Multi-threaded continuous registration with fallback, target count, and logging."""

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self._threads: list[threading.Thread] = []
        self._stop_event = threading.Event()
        self._target_reached = threading.Event()
        self._lock = threading.RLock()
        self._total_attempts = 0
        self._total_success = 0
        self._total_cpa_sync_success = 0
        self._total_cpa_sync_failure = 0
        self._total_failure = 0
        self._last_error: str | None = None
        self._started_at: float | None = None
        self._failure_by_stage: dict[str, int] = {}
        self._failure_signals: dict[str, int] = {}
        self._recent_attempts: deque[dict[str, object]] = deque(maxlen=80)
        self._providers: list[str] = []
        self._proxy_pool = None
        self._logger = self._setup_logger()
        self._cfmail_tracker = None
        self._cfmail_provisioner = None
        self._cfmail_manager: Any = None
        self._cfmail_rotation_lock = threading.Lock()
        self._cfmail_rotation_pause = threading.Event()
        self._cfmail_rotation_pause.set()
        self._cpa_management_key_cache: str | None | bool = False
        self._cfmail_add_phone_window = max(
            1,
            int(str(os.getenv("ZHUCE6_CFMAIL_ADD_PHONE_WINDOW", DEFAULT_ADD_PHONE_STOPLOSS_WINDOW)).strip() or str(DEFAULT_ADD_PHONE_STOPLOSS_WINDOW)),
        )
        self._cfmail_add_phone_threshold = max(
            1,
            int(
                str(
                    os.getenv(
                        "ZHUCE6_CFMAIL_ADD_PHONE_THRESHOLD",
                        DEFAULT_ADD_PHONE_STOPLOSS_THRESHOLD,
                    )
                ).strip()
                or str(DEFAULT_ADD_PHONE_STOPLOSS_THRESHOLD)
            ),
        )
        self._cfmail_add_phone_cooldown_seconds = max(
            1,
            int(
                str(
                    os.getenv(
                        "ZHUCE6_CFMAIL_ADD_PHONE_COOLDOWN_SECONDS",
                        DEFAULT_ADD_PHONE_STOPLOSS_COOLDOWN_SECONDS,
                    )
                ).strip()
                or str(DEFAULT_ADD_PHONE_STOPLOSS_COOLDOWN_SECONDS)
            ),
        )
        self._cfmail_add_phone_max_successes = max(
            0,
            int(
                str(
                    os.getenv(
                        "ZHUCE6_CFMAIL_ADD_PHONE_MAX_SUCCESSES",
                        DEFAULT_ADD_PHONE_STOPLOSS_MAX_SUCCESSES,
                    )
                ).strip()
                or str(DEFAULT_ADD_PHONE_STOPLOSS_MAX_SUCCESSES)
            ),
        )
        self._cfmail_add_phone_events: dict[str, deque[dict[str, object]]] = {}
        self._cfmail_add_phone_state: dict[str, object] = {
            "active_domain": "",
            "in_cooldown": False,
            "cooldown_until": 0.0,
            "last_triggered_at": "",
            "last_rotation_attempted_at": "",
            "last_reason": "",
            "last_add_phone_failures": 0,
            "last_successes": 0,
            "last_window_size": 0,
            "last_logged_at": 0.0,
        }
        self._cfmail_wait_otp_window = max(
            1,
            int(str(os.getenv("ZHUCE6_CFMAIL_WAIT_OTP_WINDOW", DEFAULT_WAIT_OTP_STOPLOSS_WINDOW)).strip() or str(DEFAULT_WAIT_OTP_STOPLOSS_WINDOW)),
        )
        self._cfmail_wait_otp_threshold = max(
            1,
            int(
                str(
                    os.getenv(
                        "ZHUCE6_CFMAIL_WAIT_OTP_THRESHOLD",
                        DEFAULT_WAIT_OTP_STOPLOSS_THRESHOLD,
                    )
                ).strip()
                or str(DEFAULT_WAIT_OTP_STOPLOSS_THRESHOLD)
            ),
        )
        self._cfmail_wait_otp_cooldown_seconds = max(
            0,
            int(
                str(
                    os.getenv(
                        "ZHUCE6_CFMAIL_WAIT_OTP_COOLDOWN_SECONDS",
                        DEFAULT_WAIT_OTP_STOPLOSS_COOLDOWN_SECONDS,
                    )
                ).strip()
                or str(DEFAULT_WAIT_OTP_STOPLOSS_COOLDOWN_SECONDS)
            ),
        )
        self._cfmail_wait_otp_events: dict[str, deque[dict[str, object]]] = {}
        self._cfmail_wait_otp_state: dict[str, object] = {
            "active_domain": "",
            "in_cooldown": False,
            "cooldown_until": 0.0,
            "last_triggered_at": "",
            "last_rotation_attempted_at": "",
            "last_reason": "",
            "last_no_message_timeouts": 0,
            "last_window_size": 0,
            "last_logged_at": 0.0,
        }
        try:
            self._cfmail_wait_otp_live_threshold = max(
                0,
                int(
                    str(
                        os.getenv(
                            "ZHUCE6_CFMAIL_WAIT_OTP_LIVE_ABORT_THRESHOLD",
                            DEFAULT_WAIT_OTP_LIVE_ABORT_THRESHOLD,
                        )
                    ).strip()
                    or str(DEFAULT_WAIT_OTP_LIVE_ABORT_THRESHOLD)
                ),
            )
        except Exception:
            self._cfmail_wait_otp_live_threshold = DEFAULT_WAIT_OTP_LIVE_ABORT_THRESHOLD
        try:
            self._cfmail_wait_otp_live_age_seconds = max(
                30,
                int(
                    str(
                        os.getenv(
                            "ZHUCE6_CFMAIL_WAIT_OTP_LIVE_ABORT_AGE_SECONDS",
                            DEFAULT_WAIT_OTP_LIVE_ABORT_AGE_SECONDS,
                        )
                    ).strip()
                    or str(DEFAULT_WAIT_OTP_LIVE_ABORT_AGE_SECONDS)
                ),
            )
        except Exception:
            self._cfmail_wait_otp_live_age_seconds = DEFAULT_WAIT_OTP_LIVE_ABORT_AGE_SECONDS
        self._cfmail_wait_otp_live_lock = threading.RLock()
        self._cfmail_wait_otp_live_progress: dict[str, dict[str, dict[str, object]]] = {}
        self._cfmail_canary_state: dict[str, object] = {
            "active_domain": "",
            "pending": False,
            "owner_thread_id": 0,
            "attempt_started_at": 0.0,
            "last_ready_at": "",
            "last_ready_reason": "",
            "last_logged_at": 0.0,
        }
        try:
            self._cfmail_start_interval_seconds = max(
                0,
                int(str(os.getenv("ZHUCE6_CFMAIL_START_INTERVAL_SECONDS", "15")).strip() or "15"),
            )
        except Exception:
            self._cfmail_start_interval_seconds = 15
        try:
            self._cfmail_max_inflight = max(
                1,
                int(str(os.getenv("ZHUCE6_CFMAIL_MAX_INFLIGHT", "2")).strip() or "2"),
            )
        except Exception:
            self._cfmail_max_inflight = 2
        self._cfmail_flow_state: dict[str, object] = {
            "inflight_by_thread": {},
            "last_started_by_domain": {},
            "last_logged_at": 0.0,
        }
        try:
            self._cfmail_fresh_domain_attempt_budget = max(
                0,
                int(
                    str(
                        os.getenv(
                            "ZHUCE6_CFMAIL_FRESH_DOMAIN_ATTEMPT_BUDGET",
                            DEFAULT_CFMAIL_FRESH_DOMAIN_ATTEMPT_BUDGET,
                        )
                    ).strip()
                    or str(DEFAULT_CFMAIL_FRESH_DOMAIN_ATTEMPT_BUDGET)
                ),
            )
        except Exception:
            self._cfmail_fresh_domain_attempt_budget = DEFAULT_CFMAIL_FRESH_DOMAIN_ATTEMPT_BUDGET
        self._cfmail_fresh_domain_state: dict[str, object] = {
            "active_domain": "",
            "completed_attempts": 0,
            "mail_seen_attempts": 0,
            "successes": 0,
            "last_triggered_at": "",
            "last_rotation_attempted_at": "",
            "last_reason": "",
        }
        # Solution B: deferred retry queue for add_phone_gate accounts
        self._pending_token_queue: list[dict[str, Any]] = []
        self._pending_token_lock = threading.Lock()
        try:
            raw_pending_retry_delay = int(
                str(os.getenv("ZHUCE6_PENDING_TOKEN_RETRY_DELAY_SECONDS", "600")).strip() or "600"
            )
        except Exception:
            raw_pending_retry_delay = 600
        # add_phone accounts are only useful if the deferred retry happens inside the
        # same short observation window as the registration loop. Cap the first retry
        # base delay so a large env value cannot postpone every retry past 5 minutes.
        self._pending_token_retry_delay_seconds = max(60, min(raw_pending_retry_delay, 60))
        try:
            self._pending_token_max_retries = max(
                1,
                int(str(os.getenv("ZHUCE6_PENDING_TOKEN_MAX_RETRIES", "3")).strip() or "3"),
            )
        except Exception:
            self._pending_token_max_retries = 3
        self._pending_token_total_enqueued = 0
        self._pending_token_total_success = 0
        self._pending_token_total_failed = 0

    def _write_runtime_state(self) -> None:
        state_file = Path(self.settings.runtime_state_file)
        try:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "register_snapshot": self.snapshot(),
                "proxy_pool": self._proxy_pool_snapshot(),
            }
            tmp_file = state_file.with_name(
                f"{state_file.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            tmp_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp_file.replace(state_file)
        except Exception as exc:
            self._log(f"[zhuce6:register] runtime state write failed: {exc}")

    def _cfmail_canary_snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "active_domain": str(self._cfmail_canary_state.get("active_domain") or ""),
                "pending": bool(self._cfmail_canary_state.get("pending")),
                "owner_thread_id": int(self._cfmail_canary_state.get("owner_thread_id") or 0),
                "attempt_started_at": float(self._cfmail_canary_state.get("attempt_started_at") or 0.0),
                "last_ready_at": str(self._cfmail_canary_state.get("last_ready_at") or ""),
                "last_ready_reason": str(self._cfmail_canary_state.get("last_ready_reason") or ""),
            }

    def _arm_cfmail_canary(self, domain: str, *, pending: bool = True) -> None:
        with self._lock:
            self._cfmail_canary_state = {
                "active_domain": str(domain or "").strip().lower(),
                "pending": bool(pending and domain),
                "owner_thread_id": 0,
                "attempt_started_at": 0.0,
                "last_ready_at": "",
                "last_ready_reason": "",
                "last_logged_at": 0.0,
            }

    def _mark_cfmail_canary_ready(self, domain: str, *, reason: str) -> None:
        domain_key = str(domain or "").strip().lower()
        if not domain_key:
            return
        with self._lock:
            current_domain = str(self._cfmail_canary_state.get("active_domain") or "").strip().lower()
            if current_domain and current_domain != domain_key:
                return
            self._cfmail_canary_state.update(
                {
                    "active_domain": domain_key,
                    "pending": False,
                    "owner_thread_id": 0,
                    "attempt_started_at": 0.0,
                    "last_ready_at": datetime.now().isoformat(timespec="seconds"),
                    "last_ready_reason": reason,
                }
            )

    def _update_cfmail_canary_after_result(self, *, thread_id: int, result: dict[str, object]) -> None:
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        provider = str(metadata.get("mail_provider") or result.get("mail_provider") or "").strip().lower()
        if provider not in {"", "cfmail"}:
            return
        domain = self._extract_email_domain(result)
        if not domain:
            return
        current_domain = self._current_cfmail_active_domain()
        if current_domain and domain != current_domain:
            return
        success = bool(result.get("success"))
        try:
            message_scan_count = int(metadata.get("otp_mailbox_message_scan_count") or 0)
        except Exception:
            message_scan_count = 0
        if success or message_scan_count > 0:
            reason = "success" if success else "mailbox_message_seen"
            self._mark_cfmail_canary_ready(domain, reason=reason)
            return
        with self._lock:
            if (
                bool(self._cfmail_canary_state.get("pending"))
                and str(self._cfmail_canary_state.get("active_domain") or "").strip().lower() == domain
                and int(self._cfmail_canary_state.get("owner_thread_id") or 0) == thread_id
            ):
                self._cfmail_canary_state["owner_thread_id"] = 0
                self._cfmail_canary_state["attempt_started_at"] = 0.0

    def _wait_if_cfmail_canary_pending(self, thread_id: int, provider: str) -> bool:
        if provider != "cfmail":
            return False
        with self._lock:
            state = dict(self._cfmail_canary_state)
            pending = bool(state.get("pending"))
            domain = str(state.get("active_domain") or "").strip().lower()
            owner_thread_id = int(state.get("owner_thread_id") or 0)
            attempt_started_at = float(state.get("attempt_started_at") or 0.0)
            if not pending or not domain:
                return False
            now = time.time()
            owner_expired = attempt_started_at > 0.0 and now - attempt_started_at >= 240.0
            if owner_thread_id in {0, thread_id} or owner_expired:
                self._cfmail_canary_state["owner_thread_id"] = thread_id
                self._cfmail_canary_state["attempt_started_at"] = now
                return False
            last_logged_at = float(self._cfmail_canary_state.get("last_logged_at") or 0.0)
            should_log = now - last_logged_at >= 15.0
            if should_log:
                self._cfmail_canary_state["last_logged_at"] = now
        if should_log:
            self._log(
                f"[zhuce6:register] [thread-{thread_id}] [cfmail] canary pending for {domain}, "
                f"owner=thread-{owner_thread_id}"
            )
        self._stop_event.wait(2)
        return True

    def _release_cfmail_flow_slot(self, thread_id: int) -> None:
        with self._lock:
            inflight_by_thread = self._cfmail_flow_state.setdefault("inflight_by_thread", {})
            if isinstance(inflight_by_thread, dict):
                inflight_by_thread.pop(thread_id, None)

    def _wait_if_cfmail_flow_throttled(self, thread_id: int, provider: str) -> bool:
        if provider != "cfmail":
            return False
        domain = self._current_cfmail_active_domain()
        if not domain:
            return False
        with self._lock:
            inflight_by_thread = self._cfmail_flow_state.setdefault("inflight_by_thread", {})
            if not isinstance(inflight_by_thread, dict):
                inflight_by_thread = {}
                self._cfmail_flow_state["inflight_by_thread"] = inflight_by_thread
            last_started_by_domain = self._cfmail_flow_state.setdefault("last_started_by_domain", {})
            if not isinstance(last_started_by_domain, dict):
                last_started_by_domain = {}
                self._cfmail_flow_state["last_started_by_domain"] = last_started_by_domain
            tracked = inflight_by_thread.get(thread_id)
            if isinstance(tracked, dict) and str(tracked.get("domain") or "").strip().lower() == domain:
                return False
            now = time.time()
            active_inflight = sum(
                1
                for value in inflight_by_thread.values()
                if isinstance(value, dict)
                and str(value.get("domain") or "").strip().lower() == domain
            )
            wait_reason = ""
            wait_seconds = 0.0
            if active_inflight >= self._cfmail_max_inflight:
                wait_reason = "inflight_limit"
                wait_seconds = 2.0
            else:
                last_started_at = float(last_started_by_domain.get(domain) or 0.0)
                if self._cfmail_start_interval_seconds > 0 and last_started_at > 0.0:
                    remaining = self._cfmail_start_interval_seconds - (now - last_started_at)
                    if remaining > 0.0:
                        wait_reason = "start_interval"
                        wait_seconds = min(2.0, max(0.5, remaining))
            if not wait_reason:
                inflight_by_thread[thread_id] = {
                    "domain": domain,
                    "started_at": now,
                }
                last_started_by_domain[domain] = now
                return False
            last_logged_at = float(self._cfmail_flow_state.get("last_logged_at") or 0.0)
            should_log = now - last_logged_at >= 15.0
            if should_log:
                self._cfmail_flow_state["last_logged_at"] = now
        if should_log:
            if wait_reason == "inflight_limit":
                self._log(
                    f"[zhuce6:register] [thread-{thread_id}] [cfmail] flow throttle for {domain}: "
                    f"inflight={active_inflight}/{self._cfmail_max_inflight}"
                )
            else:
                self._log(
                    f"[zhuce6:register] [thread-{thread_id}] [cfmail] flow throttle for {domain}: "
                    f"start_interval_remaining={wait_seconds:.1f}s"
                )
        self._stop_event.wait(wait_seconds)
        return True

    def _proxy_pool_snapshot(self) -> dict[str, object]:
        pool = self._proxy_pool
        nodes: list[dict[str, object]] = []
        snapshot_error: str | None = None
        if pool is not None:
            try:
                snapshot = pool.snapshot()
            except Exception as exc:
                snapshot_error = str(exc)
            else:
                if isinstance(snapshot, list):
                    nodes = [item for item in snapshot if isinstance(item, dict)]
        return {
            "configured": bool(self.settings.proxy_pool_configured or pool is not None),
            "enabled": pool is not None,
            "snapshot_error": snapshot_error,
            "node_count": len(nodes),
            "in_use_count": sum(1 for item in nodes if item.get("in_use")),
            "disabled_count": sum(1 for item in nodes if item.get("disabled")),
            "nodes": nodes,
        }

    def _setup_logger(self) -> Any:
        import logging
        from logging.handlers import RotatingFileHandler

        logger = logging.getLogger("zhuce6.register")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if not logger.handlers:
            console = logging.StreamHandler()
            console.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(console)
            if self.settings.register_log_file:
                fh = RotatingFileHandler(
                    self.settings.register_log_file,
                    maxBytes=2 * 1024 * 1024,  # 2MB
                    backupCount=5,
                    encoding="utf-8",
                )
                fh.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
                logger.addHandler(fh)
        return logger

    def _log(self, msg: str) -> None:
        self._logger.info(msg)

    def _record_attempt(
        self,
        *,
        success: bool,
        stage: str,
        error_message: str,
        metadata: dict[str, object] | None = None,
        proxy_key: str = "",
        email: str = "",
    ) -> None:
        meta = metadata if isinstance(metadata, dict) else {}
        stage_key = str(stage or "?").strip() or "?"
        signal = self._classify_failure_signal(stage=stage_key, metadata=meta)
        timestamp = datetime.now().isoformat(timespec="seconds")
        event = {
            "timestamp": timestamp,
            "success": success,
            "stage": stage_key if not success else "completed",
            "signal": signal,
            "error_message": str(error_message or "").strip(),
            "email_domain": str(meta.get("email_domain") or "").strip(),
            "post_create_gate": str(meta.get("post_create_gate") or "").strip(),
            "create_account_error_code": str(meta.get("create_account_error_code") or "").strip(),
            "proxy_key": proxy_key,
            "email": email,
        }
        self._recent_attempts.append(event)
        if success:
            return
        self._failure_by_stage[stage_key] = self._failure_by_stage.get(stage_key, 0) + 1
        if signal:
            self._failure_signals[signal] = self._failure_signals.get(signal, 0) + 1

    def _classify_failure_signal(self, *, stage: str, metadata: dict[str, object]) -> str:
        code = str(metadata.get("create_account_error_code") or "").strip().lower()
        post_gate = str(metadata.get("post_create_gate") or "").strip().lower()
        if stage == "cpa_sync":
            return "cpa_sync_failed"
        if stage == "add_phone_gate" or post_gate == "add_phone":
            return "add_phone_gate"
        if stage == "create_account" and code == "user_already_exists":
            return "mailbox_reused"
        if stage == "create_account" and code in {"registration_disallowed", "unsupported_email"}:
            return code
        if stage == "mailbox":
            provider = str(metadata.get("mail_provider") or "").strip().lower()
            if provider == "cfmail":
                return "mailbox_backend_failure"
            return "mailbox_failure"
        return ""

    def _recent_failure_hotspots(self, limit: int = 5) -> list[dict[str, object]]:
        return self._recent_failure_hotspots_from_attempts(self._recent_attempts, limit=limit)

    def _recent_failure_hotspots_from_attempts(
        self,
        attempts: list[dict[str, object]] | deque[dict[str, object]],
        *,
        limit: int = 5,
    ) -> list[dict[str, object]]:
        counts: dict[tuple[str, str], int] = {}
        for item in attempts:
            if item.get("success"):
                continue
            stage = str(item.get("stage") or "?").strip() or "?"
            signal = str(item.get("signal") or "").strip()
            key = (signal or stage, stage)
            counts[key] = counts.get(key, 0) + 1
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1]))
        return [
            {"key": key, "stage": stage, "count": count}
            for (key, stage), count in ordered[:limit]
        ]

    def _failure_counts_from_attempts(
        self,
        attempts: list[dict[str, object]] | deque[dict[str, object]],
    ) -> tuple[dict[str, int], dict[str, int]]:
        failure_by_stage: dict[str, int] = {}
        failure_signals: dict[str, int] = {}
        for item in attempts:
            if item.get("success"):
                continue
            stage = str(item.get("stage") or "?").strip() or "?"
            failure_by_stage[stage] = failure_by_stage.get(stage, 0) + 1
            signal = str(item.get("signal") or "").strip()
            if signal:
                failure_signals[signal] = failure_signals.get(signal, 0) + 1
        return (
            dict(sorted(failure_by_stage.items(), key=lambda item: (-item[1], item[0]))),
            dict(sorted(failure_signals.items(), key=lambda item: (-item[1], item[0]))),
        )

    def _active_domain_attempts(
        self,
        recent_attempts: list[dict[str, object]],
        active_domain: str,
    ) -> list[dict[str, object]]:
        domain = str(active_domain or "").strip().lower()
        if not domain:
            return list(recent_attempts)
        filtered = [
            item
            for item in recent_attempts
            if str(item.get("email_domain") or "").strip().lower() == domain
        ]
        return filtered

    def _infer_active_domain(
        self,
        recent_attempts: list[dict[str, object]],
        cfmail_rotation: dict[str, object] | None,
        stoploss: dict[str, object],
    ) -> str:
        if isinstance(cfmail_rotation, dict):
            domain = str(cfmail_rotation.get("active_domain") or "").strip().lower()
            if domain:
                return domain
        domain = str(stoploss.get("active_domain") or "").strip().lower()
        if domain:
            return domain
        for item in reversed(recent_attempts):
            domain = str(item.get("email_domain") or "").strip().lower()
            if domain:
                return domain
        return ""

    def _extract_email_domain(self, result: dict[str, object]) -> str:
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        domain = str(metadata.get("email_domain") or "").strip().lower()
        if domain:
            return domain
        email = str(result.get("email") or "").strip().lower()
        if "@" not in email:
            return ""
        return email.rsplit("@", 1)[-1].strip().lower()

    def _update_cfmail_add_phone_stoploss(self, result: dict[str, object]) -> None:
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        provider = str(metadata.get("mail_provider") or result.get("mail_provider") or "").strip().lower()
        if provider not in {"", "cfmail"}:
            return
        domain = self._extract_email_domain(result)
        if not domain:
            return
        current_domain = self._current_cfmail_active_domain()
        if current_domain and domain != current_domain:
            return
        success = bool(result.get("success"))
        stage = str(result.get("stage") or "").strip().lower()
        post_gate = str(metadata.get("post_create_gate") or "").strip().lower()
        is_add_phone = stage == "add_phone_gate" or post_gate == "add_phone"
        with self._lock:
            events = self._cfmail_add_phone_events.setdefault(
                domain,
                deque(maxlen=self._cfmail_add_phone_window),
            )
            events.append(
                {
                    "success": success,
                    "is_add_phone": is_add_phone,
                }
            )
            state = self._cfmail_add_phone_state
            state["active_domain"] = domain
            if self._cfmail_add_phone_cooldown_seconds <= 0:
                state["in_cooldown"] = False
                state["cooldown_until"] = 0.0
                return
            cooldown_until = float(state.get("cooldown_until") or 0.0)
            if time.time() < cooldown_until:
                state["in_cooldown"] = True
                return
            state["in_cooldown"] = False
            if len(events) < self._cfmail_add_phone_window:
                return
            add_phone_failures = sum(1 for item in events if item.get("is_add_phone"))
            successes = sum(1 for item in events if item.get("success"))
            if (
                add_phone_failures >= self._cfmail_add_phone_threshold
                and successes <= self._cfmail_add_phone_max_successes
            ):
                state["in_cooldown"] = True
                state["cooldown_until"] = time.time() + self._cfmail_add_phone_cooldown_seconds
                state["last_triggered_at"] = datetime.now().isoformat(timespec="seconds")
                state["last_rotation_attempted_at"] = ""
                state["last_reason"] = "add_phone threshold reached"
                state["last_add_phone_failures"] = add_phone_failures
                state["last_successes"] = successes
                state["last_window_size"] = len(events)
                self._log(
                    f"[zhuce6:register] [cfmail] add_phone stoploss activated for {domain} "
                    f"(add_phone_failures={add_phone_failures}, successes={successes}, window={len(events)})"
                )

    def _is_cfmail_wait_otp_no_message_timeout(self, result: dict[str, object]) -> bool:
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        provider = str(metadata.get("mail_provider") or result.get("mail_provider") or "").strip().lower()
        if provider not in {"", "cfmail"}:
            return False
        stage = str(result.get("stage") or "").strip().lower()
        if stage != "wait_otp":
            return False
        failure_reason = str(metadata.get("otp_wait_failure_reason") or "").strip().lower()
        if failure_reason:
            return failure_reason == "mailbox_timeout_no_message"
        try:
            return int(metadata.get("otp_mailbox_message_scan_count") or 0) <= 0
        except Exception:
            return False

    def _is_cfmail_invalid_domain_mailbox_failure(self, result: dict[str, object]) -> bool:
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        if str(result.get("stage") or "").strip().lower() != "mailbox":
            return False
        provider = str(metadata.get("mail_provider") or result.get("mail_provider") or "").strip().lower()
        if provider not in {"", "cfmail"}:
            return False
        haystacks = [str(result.get("error_message") or "")]
        haystacks.extend(str(item or "") for item in (result.get("logs") or []))
        text = "\n".join(haystacks).lower()
        return "invalid domain" in text or "无效的域名" in text

    def _on_cfmail_wait_progress(self, account: object, diagnostics: dict[str, object]) -> None:
        if self._cfmail_wait_otp_live_threshold <= 0:
            return
        email = str(getattr(account, "email", "") or "").strip().lower()
        extra = getattr(account, "extra", {}) or {}
        domain = str(extra.get("email_domain") or "").strip().lower()
        if not domain and "@" in email:
            domain = email.rsplit("@", 1)[-1].strip().lower()
        if not domain:
            return
        current_domain = self._current_cfmail_active_domain()
        with self._cfmail_wait_otp_live_lock:
            now = time.time()
            for tracked_domain in list(self._cfmail_wait_otp_live_progress.keys()):
                entries = self._cfmail_wait_otp_live_progress.get(tracked_domain) or {}
                fresh_entries = {
                    key: value
                    for key, value in entries.items()
                    if now - float(value.get("updated_at") or 0.0) <= 15.0
                }
                if fresh_entries:
                    self._cfmail_wait_otp_live_progress[tracked_domain] = fresh_entries
                else:
                    self._cfmail_wait_otp_live_progress.pop(tracked_domain, None)
            if current_domain and domain != current_domain:
                self._cfmail_wait_otp_live_progress.pop(domain, None)
                return
            key = str(getattr(account, "account_id", "") or email or id(account))
            domain_entries = self._cfmail_wait_otp_live_progress.setdefault(domain, {})
            domain_entries[key] = {
                "scan_count": int(diagnostics.get("message_scan_count") or 0),
                "elapsed_seconds": float(diagnostics.get("elapsed_seconds") or 0.0),
                "updated_at": now,
            }
            if int(diagnostics.get("message_scan_count") or 0) > 0:
                self._mark_cfmail_canary_ready(domain, reason="live_mailbox_message_seen")
            state = self._cfmail_wait_otp_state
            if bool(state.get("in_cooldown")) and str(state.get("active_domain") or "").strip().lower() == domain:
                return
            stalled = [
                value
                for value in domain_entries.values()
                if int(value.get("scan_count") or 0) <= 0
                and float(value.get("elapsed_seconds") or 0.0) >= self._cfmail_wait_otp_live_age_seconds
            ]
            if len(stalled) < self._cfmail_wait_otp_live_threshold:
                return
        with self._lock:
            state = self._cfmail_wait_otp_state
            if bool(state.get("in_cooldown")) and str(state.get("active_domain") or "").strip().lower() == domain:
                return
            self._cfmail_wait_otp_state = {
                "active_domain": domain,
                "in_cooldown": True,
                "cooldown_until": time.time() + self._cfmail_wait_otp_cooldown_seconds,
                "last_triggered_at": now_iso(),
                "last_rotation_attempted_at": "",
                "last_reason": "live wait_otp no-message threshold reached",
                "last_no_message_timeouts": len(stalled),
                "last_window_size": len(stalled),
                "last_logged_at": 0.0,
            }
        self._log(
            f"[zhuce6:register] [cfmail] wait_otp live stoploss activated for {domain} "
            f"(stalled_waits={len(stalled)}, age>={self._cfmail_wait_otp_live_age_seconds}s)"
        )

    def _update_cfmail_wait_otp_stoploss(self, result: dict[str, object]) -> None:
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        provider = str(metadata.get("mail_provider") or result.get("mail_provider") or "").strip().lower()
        if provider not in {"", "cfmail"}:
            return
        domain = self._extract_email_domain(result)
        if not domain:
            return
        current_domain = self._current_cfmail_active_domain()
        if current_domain and domain != current_domain:
            return
        is_no_message_timeout = self._is_cfmail_wait_otp_no_message_timeout(result)
        try:
            message_scan_count = int(metadata.get("otp_mailbox_message_scan_count") or 0)
        except Exception:
            message_scan_count = 0
        has_message_seen = message_scan_count > 0
        with self._lock:
            events = self._cfmail_wait_otp_events.setdefault(
                domain,
                deque(maxlen=self._cfmail_wait_otp_window),
            )
            events.append(
                {
                    "success": bool(result.get("success")),
                    "is_no_message_timeout": is_no_message_timeout,
                    "has_message_seen": has_message_seen,
                }
            )
            state = self._cfmail_wait_otp_state
            state["active_domain"] = domain
            if self._cfmail_wait_otp_cooldown_seconds <= 0:
                state["in_cooldown"] = False
                state["cooldown_until"] = 0.0
                return
            cooldown_until = float(state.get("cooldown_until") or 0.0)
            if time.time() < cooldown_until:
                state["in_cooldown"] = True
                return
            state["in_cooldown"] = False
            if len(events) < self._cfmail_wait_otp_window:
                return
            no_message_timeouts = sum(1 for item in events if item.get("is_no_message_timeout"))
            successes = sum(1 for item in events if item.get("success"))
            message_seen = sum(1 for item in events if item.get("has_message_seen"))
            if (
                no_message_timeouts >= self._cfmail_wait_otp_threshold
                and successes <= 0
                and message_seen <= 0
            ):
                state["in_cooldown"] = True
                state["cooldown_until"] = time.time() + self._cfmail_wait_otp_cooldown_seconds
                state["last_triggered_at"] = datetime.now().isoformat(timespec="seconds")
                state["last_rotation_attempted_at"] = ""
                state["last_reason"] = "wait_otp no-message threshold reached"
                state["last_no_message_timeouts"] = no_message_timeouts
                state["last_successes"] = successes
                state["last_message_seen"] = message_seen
                state["last_window_size"] = len(events)
                self._log(
                    f"[zhuce6:register] [cfmail] wait_otp stoploss activated for {domain} "
                    f"(no_message_timeouts={no_message_timeouts}, successes={successes}, "
                    f"message_seen={message_seen}, window={len(events)})"
                )

    def _update_cfmail_fresh_domain_budget(self, result: dict[str, object]) -> None:
        if self._cfmail_fresh_domain_attempt_budget <= 0:
            return
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        provider = str(metadata.get("mail_provider") or result.get("mail_provider") or "").strip().lower()
        if provider not in {"", "cfmail"}:
            return
        domain = self._extract_email_domain(result)
        if not domain:
            return
        current_domain = self._current_cfmail_active_domain()
        if current_domain and domain != current_domain:
            return
        try:
            message_scan_count = int(metadata.get("otp_mailbox_message_scan_count") or 0)
        except Exception:
            message_scan_count = 0
        success = bool(result.get("success"))
        with self._lock:
            state = self._cfmail_fresh_domain_state
            tracked_domain = str(state.get("active_domain") or "").strip().lower()
            if tracked_domain != domain:
                self._cfmail_fresh_domain_state = {
                    "active_domain": domain,
                    "completed_attempts": 0,
                    "mail_seen_attempts": 0,
                    "successes": 0,
                    "last_triggered_at": "",
                    "last_rotation_attempted_at": "",
                    "last_reason": "",
                }
                state = self._cfmail_fresh_domain_state
            state["completed_attempts"] = int(state.get("completed_attempts") or 0) + 1
            if message_scan_count > 0:
                state["mail_seen_attempts"] = int(state.get("mail_seen_attempts") or 0) + 1
            if success:
                state["successes"] = int(state.get("successes") or 0) + 1
            if (
                int(state.get("mail_seen_attempts") or 0) > 0
                and int(state.get("completed_attempts") or 0) >= self._cfmail_fresh_domain_attempt_budget
                and not str(state.get("last_rotation_attempted_at") or "").strip()
            ):
                state["last_triggered_at"] = datetime.now().isoformat(timespec="seconds")
                state["last_reason"] = "fresh_domain_attempt_budget_reached"
                self._log(
                    f"[zhuce6:register] [cfmail] fresh-domain budget reached for {domain} "
                    f"(completed_attempts={int(state.get('completed_attempts') or 0)}, "
                    f"mail_seen_attempts={int(state.get('mail_seen_attempts') or 0)}, "
                    f"budget={self._cfmail_fresh_domain_attempt_budget})"
                )

    def _cfmail_add_phone_stoploss_snapshot(self) -> dict[str, object]:
        with self._lock:
            state = dict(self._cfmail_add_phone_state)
            if self._cfmail_add_phone_cooldown_seconds <= 0:
                return {
                    "active_domain": str(state.get("active_domain") or ""),
                    "in_cooldown": False,
                    "cooldown_remaining_seconds": 0,
                    "last_triggered_at": str(state.get("last_triggered_at") or ""),
                    "last_reason": str(state.get("last_reason") or ""),
                    "last_add_phone_failures": int(state.get("last_add_phone_failures") or 0),
                    "last_successes": int(state.get("last_successes") or 0),
                    "last_window_size": int(state.get("last_window_size") or 0),
                    "window_size": self._cfmail_add_phone_window,
                    "threshold": self._cfmail_add_phone_threshold,
                    "max_successes_in_window": self._cfmail_add_phone_max_successes,
                }
            cooldown_until = float(state.get("cooldown_until") or 0.0)
            remaining = max(0, int(cooldown_until - time.time()))
            if remaining <= 0:
                state["in_cooldown"] = False
            return {
                "active_domain": str(state.get("active_domain") or ""),
                "in_cooldown": bool(state.get("in_cooldown")),
                "cooldown_remaining_seconds": remaining,
                "last_triggered_at": str(state.get("last_triggered_at") or ""),
                "last_reason": str(state.get("last_reason") or ""),
                "last_add_phone_failures": int(state.get("last_add_phone_failures") or 0),
                "last_successes": int(state.get("last_successes") or 0),
                "last_window_size": int(state.get("last_window_size") or 0),
                "window_size": self._cfmail_add_phone_window,
                "threshold": self._cfmail_add_phone_threshold,
                "max_successes_in_window": self._cfmail_add_phone_max_successes,
            }

    def _reset_cfmail_add_phone_stoploss(self, new_domain: str = "") -> None:
        with self._lock:
            self._cfmail_add_phone_state = {
                "active_domain": new_domain,
                "in_cooldown": False,
                "cooldown_until": 0.0,
                "last_triggered_at": "",
                "last_rotation_attempted_at": "",
                "last_reason": "",
                "last_add_phone_failures": 0,
                "last_successes": 0,
                "last_window_size": 0,
                "last_logged_at": 0.0,
            }

    def _cfmail_wait_otp_stoploss_snapshot(self) -> dict[str, object]:
        with self._lock:
            state = dict(self._cfmail_wait_otp_state)
            if self._cfmail_wait_otp_cooldown_seconds <= 0:
                return {
                    "active_domain": str(state.get("active_domain") or ""),
                    "in_cooldown": False,
                    "cooldown_remaining_seconds": 0,
                    "last_triggered_at": str(state.get("last_triggered_at") or ""),
                    "last_reason": str(state.get("last_reason") or ""),
                    "last_no_message_timeouts": int(state.get("last_no_message_timeouts") or 0),
                    "last_successes": int(state.get("last_successes") or 0),
                    "last_message_seen": int(state.get("last_message_seen") or 0),
                    "last_window_size": int(state.get("last_window_size") or 0),
                    "window_size": self._cfmail_wait_otp_window,
                    "threshold": self._cfmail_wait_otp_threshold,
                }
            cooldown_until = float(state.get("cooldown_until") or 0.0)
            remaining = max(0, int(cooldown_until - time.time()))
            if remaining <= 0:
                state["in_cooldown"] = False
            return {
                "active_domain": str(state.get("active_domain") or ""),
                "in_cooldown": bool(state.get("in_cooldown")),
                "cooldown_remaining_seconds": remaining,
                "last_triggered_at": str(state.get("last_triggered_at") or ""),
                "last_reason": str(state.get("last_reason") or ""),
                "last_no_message_timeouts": int(state.get("last_no_message_timeouts") or 0),
                "last_successes": int(state.get("last_successes") or 0),
                "last_message_seen": int(state.get("last_message_seen") or 0),
                "last_window_size": int(state.get("last_window_size") or 0),
                "window_size": self._cfmail_wait_otp_window,
                "threshold": self._cfmail_wait_otp_threshold,
            }

    def _cfmail_fresh_domain_budget_snapshot(self) -> dict[str, object]:
        with self._lock:
            state = dict(self._cfmail_fresh_domain_state)
            return {
                "active_domain": str(state.get("active_domain") or ""),
                "completed_attempts": int(state.get("completed_attempts") or 0),
                "mail_seen_attempts": int(state.get("mail_seen_attempts") or 0),
                "successes": int(state.get("successes") or 0),
                "last_triggered_at": str(state.get("last_triggered_at") or ""),
                "last_rotation_attempted_at": str(state.get("last_rotation_attempted_at") or ""),
                "last_reason": str(state.get("last_reason") or ""),
                "attempt_budget": self._cfmail_fresh_domain_attempt_budget,
            }

    def _reset_cfmail_wait_otp_stoploss(self, new_domain: str = "") -> None:
        with self._lock:
            self._cfmail_wait_otp_state = {
                "active_domain": new_domain,
                "in_cooldown": False,
                "cooldown_until": 0.0,
                "last_triggered_at": "",
                "last_rotation_attempted_at": "",
                "last_reason": "",
                "last_no_message_timeouts": 0,
                "last_successes": 0,
                "last_message_seen": 0,
                "last_window_size": 0,
                "last_logged_at": 0.0,
            }
        with self._cfmail_wait_otp_live_lock:
            if new_domain:
                self._cfmail_wait_otp_live_progress = {
                    str(new_domain).strip().lower(): {}
                }
            else:
                self._cfmail_wait_otp_live_progress = {}

    def _reset_cfmail_fresh_domain_budget(self, new_domain: str = "") -> None:
        with self._lock:
            self._cfmail_fresh_domain_state = {
                "active_domain": str(new_domain or "").strip().lower(),
                "completed_attempts": 0,
                "mail_seen_attempts": 0,
                "successes": 0,
                "last_triggered_at": "",
                "last_rotation_attempted_at": "",
                "last_reason": "",
            }

    def _reload_cfmail_manager_after_rotation(self) -> None:
        manager = self._cfmail_manager
        if manager is None:
            return
        try:
            reload_if_needed = getattr(manager, "reload_if_needed", None)
            if callable(reload_if_needed):
                try:
                    reload_if_needed(force=True)
                except TypeError:
                    reload_if_needed()
        except Exception as exc:
            self._log(f"[zhuce6:register] [cfmail] manager reload after rotation failed: {exc}")

    def _ensure_cfmail_active_domain_ready(self) -> bool:
        if self._cfmail_provisioner is None or self._cfmail_manager is None:
            return False
        from core.base_mailbox import create_mailbox

        active_domain = self._current_cfmail_active_domain()
        if not active_domain:
            return False
        mailbox = create_mailbox("cfmail", proxy=self.settings.register_proxy)
        try:
            account = mailbox.get_email()
            email = str(getattr(account, "email", "") or "").strip()
            self._log(
                f"[zhuce6:register] [cfmail] startup active-domain preflight ok: "
                f"{active_domain} ({email or 'mailbox-created'})"
            )
            self._arm_cfmail_canary(active_domain)
            return True
        except Exception as exc:
            message = str(exc or "").strip()
            if "无效的域名" not in message and "invalid domain" not in message.lower():
                self._log(
                    f"[zhuce6:register] [cfmail] startup active-domain preflight skipped: {message or 'unknown error'}"
                )
                return False
            self._log(
                f"[zhuce6:register] [cfmail] startup active domain invalid, rotating: "
                f"{active_domain} | {message}"
            )
            provision_result = self._cfmail_provisioner.rotate_active_domain()
            if not provision_result.success:
                self._log(
                    f"[zhuce6:register] [cfmail] startup rotation failed: {provision_result.error}"
                )
                return False
            if self._cfmail_tracker is not None:
                self._cfmail_tracker.mark_rotation_completed(
                    provision_result.old_domain,
                    provision_result.new_domain,
                )
            self._reload_cfmail_manager_after_rotation()
            self._reset_cfmail_add_phone_stoploss(provision_result.new_domain)
            self._reset_cfmail_wait_otp_stoploss(provision_result.new_domain)
            self._reset_cfmail_fresh_domain_budget(provision_result.new_domain)
            self._arm_cfmail_canary(provision_result.new_domain)
            self._log(
                f"[zhuce6:register] [cfmail] startup rotation completed: "
                f"{provision_result.old_domain} -> {provision_result.new_domain}"
            )
            return True

    def _force_rotate_cfmail_for_invalid_mailbox(self, thread_id: int, result: dict[str, object]) -> bool:
        if not self._is_cfmail_invalid_domain_mailbox_failure(result):
            return False
        if self._cfmail_tracker is None or self._cfmail_provisioner is None:
            return False
        if not self._cfmail_rotation_lock.acquire(blocking=False):
            self._log(f"[zhuce6:register] [thread-{thread_id}] [cfmail] rotation already in progress")
            return False
        self._cfmail_rotation_pause.clear()
        current_domain = self._current_cfmail_active_domain()
        try:
            if self._cfmail_tracker is not None and current_domain:
                self._cfmail_tracker.mark_rotation_started(current_domain, "mailbox invalid domain")
            self._log(
                f"[zhuce6:register] [thread-{thread_id}] [cfmail] rotating invalid active domain "
                f"{current_domain or '-'} after mailbox bootstrap failure"
            )
            provision_result = self._cfmail_provisioner.rotate_active_domain()
            if not provision_result.success:
                if self._cfmail_tracker is not None and current_domain:
                    self._cfmail_tracker.mark_rotation_failed(current_domain, provision_result.error)
                self._log(
                    f"[zhuce6:register] [thread-{thread_id}] [cfmail] invalid-domain rotation failed: "
                    f"{provision_result.error}"
                )
                return False
            if self._cfmail_tracker is not None:
                self._cfmail_tracker.mark_rotation_completed(
                    provision_result.old_domain,
                    provision_result.new_domain,
                )
            self._reload_cfmail_manager_after_rotation()
            self._reset_cfmail_add_phone_stoploss(provision_result.new_domain)
            self._reset_cfmail_wait_otp_stoploss(provision_result.new_domain)
            self._reset_cfmail_fresh_domain_budget(provision_result.new_domain)
            self._arm_cfmail_canary(provision_result.new_domain)
            self._log(
                f"[zhuce6:register] [thread-{thread_id}] [cfmail] invalid-domain rotation completed: "
                f"{provision_result.old_domain} -> {provision_result.new_domain}"
            )
            return True
        finally:
            self._cfmail_rotation_pause.set()
            self._cfmail_rotation_lock.release()

    def _rotate_cfmail_for_failed_canary(self, thread_id: int, result: dict[str, object]) -> bool:
        if not self._is_cfmail_wait_otp_no_message_timeout(result):
            return False
        if self._cfmail_tracker is None or self._cfmail_provisioner is None:
            return False
        domain = self._extract_email_domain(result)
        current_domain = self._current_cfmail_active_domain()
        if not domain or not current_domain or domain != current_domain:
            return False
        with self._lock:
            canary_domain = str(self._cfmail_canary_state.get("active_domain") or "").strip().lower()
            canary_pending = bool(self._cfmail_canary_state.get("pending"))
            canary_owner_thread_id = int(self._cfmail_canary_state.get("owner_thread_id") or 0)
        if not canary_pending or canary_domain != domain or canary_owner_thread_id != thread_id:
            return False
        if not self._cfmail_rotation_lock.acquire(blocking=False):
            return False
        self._cfmail_rotation_pause.clear()
        try:
            self._cfmail_tracker.mark_rotation_started(domain, "canary wait_otp failure")
            self._log(
                f"[zhuce6:register] [thread-{thread_id}] [cfmail] rotating domain {domain} "
                f"because canary wait_otp failure"
            )
            provision_result = self._cfmail_provisioner.rotate_active_domain()
            if not provision_result.success:
                self._cfmail_tracker.mark_rotation_failed(domain, provision_result.error)
                self._log(
                    f"[zhuce6:register] [thread-{thread_id}] [cfmail] canary rotation failed: "
                    f"{provision_result.error}"
                )
                return False
            self._cfmail_tracker.mark_rotation_completed(
                provision_result.old_domain,
                provision_result.new_domain,
            )
            self._reload_cfmail_manager_after_rotation()
            self._reset_cfmail_add_phone_stoploss(provision_result.new_domain)
            self._reset_cfmail_wait_otp_stoploss(provision_result.new_domain)
            self._reset_cfmail_fresh_domain_budget(provision_result.new_domain)
            self._arm_cfmail_canary(provision_result.new_domain)
            self._log(
                f"[zhuce6:register] [thread-{thread_id}] [cfmail] canary rotation completed: "
                f"{provision_result.old_domain} -> {provision_result.new_domain}"
            )
            return True
        finally:
            self._cfmail_rotation_pause.set()
            self._cfmail_rotation_lock.release()

    def _rotate_cfmail_for_fresh_domain_budget(self, thread_id: int) -> bool:
        if self._cfmail_fresh_domain_attempt_budget <= 0:
            return False
        if self._cfmail_tracker is None or self._cfmail_provisioner is None:
            return False
        with self._lock:
            state = dict(self._cfmail_fresh_domain_state)
            domain = str(state.get("active_domain") or "").strip().lower()
            completed_attempts = int(state.get("completed_attempts") or 0)
            mail_seen_attempts = int(state.get("mail_seen_attempts") or 0)
            if (
                not domain
                or mail_seen_attempts <= 0
                or completed_attempts < self._cfmail_fresh_domain_attempt_budget
                or str(state.get("last_rotation_attempted_at") or "").strip()
            ):
                return False
            self._cfmail_fresh_domain_state["last_rotation_attempted_at"] = datetime.now().isoformat(timespec="seconds")
        current_domain = self._current_cfmail_active_domain()
        if current_domain and domain != current_domain:
            self._reset_cfmail_fresh_domain_budget(current_domain)
            return False
        if not self._cfmail_rotation_lock.acquire(blocking=False):
            return False
        self._cfmail_rotation_pause.clear()
        try:
            self._cfmail_tracker.mark_rotation_started(domain, "fresh domain budget reached")
            self._log(
                f"[zhuce6:register] [thread-{thread_id}] [cfmail] rotating domain {domain} "
                f"because fresh domain budget reached"
            )
            provision_result = self._cfmail_provisioner.rotate_active_domain()
            if not provision_result.success:
                self._cfmail_tracker.mark_rotation_failed(domain, provision_result.error)
                with self._lock:
                    self._cfmail_fresh_domain_state["last_rotation_attempted_at"] = ""
                self._log(
                    f"[zhuce6:register] [thread-{thread_id}] [cfmail] fresh-domain rotation failed: "
                    f"{provision_result.error}"
                )
                return False
            self._cfmail_tracker.mark_rotation_completed(
                provision_result.old_domain,
                provision_result.new_domain,
            )
            self._reload_cfmail_manager_after_rotation()
            self._reset_cfmail_add_phone_stoploss(provision_result.new_domain)
            self._reset_cfmail_wait_otp_stoploss(provision_result.new_domain)
            self._reset_cfmail_fresh_domain_budget(provision_result.new_domain)
            self._arm_cfmail_canary(provision_result.new_domain)
            self._log(
                f"[zhuce6:register] [thread-{thread_id}] [cfmail] fresh-domain rotation completed: "
                f"{provision_result.old_domain} -> {provision_result.new_domain}"
            )
            return True
        finally:
            self._cfmail_rotation_pause.set()
            self._cfmail_rotation_lock.release()

    def _rotate_cfmail_for_stoploss(
        self,
        *,
        thread_id: int,
        state_attr: str,
        reason_label: str,
    ) -> bool:
        if self._cfmail_tracker is None or self._cfmail_provisioner is None:
            return False
        with self._lock:
            state_obj = getattr(self, state_attr, None)
            if not isinstance(state_obj, dict):
                return False
            domain = str(state_obj.get("active_domain") or "").strip().lower()
            if not state_obj.get("in_cooldown") or not domain:
                return False
            if str(state_obj.get("last_rotation_attempted_at") or "").strip():
                return False
            state_obj["last_rotation_attempted_at"] = datetime.now().isoformat(timespec="seconds")
        if not self._cfmail_rotation_lock.acquire(blocking=False):
            return False
        self._cfmail_rotation_pause.clear()
        try:
            self._cfmail_tracker.mark_rotation_started(domain, reason_label)
            self._log(
                f"[zhuce6:register] [thread-{thread_id}] [cfmail] rotating domain {domain} "
                f"because {reason_label}"
            )
            provision_result = self._cfmail_provisioner.rotate_active_domain()
            if not provision_result.success:
                self._cfmail_tracker.mark_rotation_failed(domain, provision_result.error)
                self._log(
                    f"[zhuce6:register] [thread-{thread_id}] [cfmail] {reason_label} rotation failed: "
                    f"{provision_result.error}"
                )
                return False
            self._cfmail_tracker.mark_rotation_completed(
                provision_result.old_domain,
                provision_result.new_domain,
            )
            self._reload_cfmail_manager_after_rotation()
            self._reset_cfmail_add_phone_stoploss(provision_result.new_domain)
            self._reset_cfmail_wait_otp_stoploss(provision_result.new_domain)
            self._reset_cfmail_fresh_domain_budget(provision_result.new_domain)
            self._arm_cfmail_canary(provision_result.new_domain)
            self._log(
                f"[zhuce6:register] [thread-{thread_id}] [cfmail] {reason_label} rotation completed: "
                f"{provision_result.old_domain} -> {provision_result.new_domain}"
            )
            return True
        finally:
            self._cfmail_rotation_pause.set()
            self._cfmail_rotation_lock.release()

    def _wait_if_cfmail_add_phone_stopped(self, thread_id: int, provider: str) -> bool:
        if provider != "cfmail":
            return False
        if self._cfmail_add_phone_cooldown_seconds <= 0:
            return False
        state = self._cfmail_add_phone_stoploss_snapshot()
        if not state.get("in_cooldown"):
            return False
        current_domain = self._current_cfmail_active_domain()
        if current_domain and str(state.get("active_domain") or "").strip().lower() != current_domain:
            self._reset_cfmail_add_phone_stoploss(current_domain)
            return False
        if self._rotate_cfmail_for_stoploss(
            thread_id=thread_id,
            state_attr="_cfmail_add_phone_state",
            reason_label="add_phone stoploss",
        ):
            return True
        remaining = int(state.get("cooldown_remaining_seconds") or 0)
        domain = str(state.get("active_domain") or "")
        should_log = False
        with self._lock:
            last_logged_at = float(self._cfmail_add_phone_state.get("last_logged_at") or 0.0)
            now = time.time()
            if now - last_logged_at >= 15:
                self._cfmail_add_phone_state["last_logged_at"] = now
                should_log = True
        if should_log:
            self._log(
                f"[zhuce6:register] [thread-{thread_id}] [cfmail] add_phone stoploss active for {domain}, "
                f"remaining={remaining}s"
            )
        wait_seconds = min(max(remaining, 1), 5)
        self._stop_event.wait(wait_seconds)
        return True

    def _wait_if_cfmail_wait_otp_stopped(self, thread_id: int, provider: str) -> bool:
        if provider != "cfmail":
            return False
        if self._cfmail_wait_otp_cooldown_seconds <= 0:
            return False
        state = self._cfmail_wait_otp_stoploss_snapshot()
        if not state.get("in_cooldown"):
            return False
        current_domain = self._current_cfmail_active_domain()
        if current_domain and str(state.get("active_domain") or "").strip().lower() != current_domain:
            self._reset_cfmail_wait_otp_stoploss(current_domain)
            return False
        if self._rotate_cfmail_for_stoploss(
            thread_id=thread_id,
            state_attr="_cfmail_wait_otp_state",
            reason_label="wait_otp stoploss",
        ):
            return True
        remaining = int(state.get("cooldown_remaining_seconds") or 0)
        domain = str(state.get("active_domain") or "")
        should_log = False
        with self._lock:
            last_logged_at = float(self._cfmail_wait_otp_state.get("last_logged_at") or 0.0)
            now = time.time()
            if now - last_logged_at >= 15:
                self._cfmail_wait_otp_state["last_logged_at"] = now
                should_log = True
        if should_log:
            self._log(
                f"[zhuce6:register] [thread-{thread_id}] [cfmail] wait_otp stoploss active for {domain}, "
                f"remaining={remaining}s"
            )
        wait_seconds = min(max(remaining, 1), 5)
        self._stop_event.wait(wait_seconds)
        return True

    def start(self) -> None:
        if self._threads:
            return
        _compat_main_attr("load_all", load_all)()
        self._stop_event.clear()
        self._target_reached.clear()
        self._started_at = time.time()
        num = self.settings.register_threads
        self._providers = [p.strip() for p in self.settings.register_mail_provider.split(",") if p.strip()]
        if not self._providers:
            self._providers = ["cfmail"]
        if "cfmail" in self._providers:
            from core.cfmail_domain_rotation import DomainHealthTracker
            import core.cfmail as cfmail_module
            from core.cfmail import DEFAULT_CFMAIL_MANAGER
            from core.cfmail_provisioner import CfmailProvisioner

            self._cfmail_tracker = DomainHealthTracker()
            self._cfmail_provisioner = CfmailProvisioner(proxy_url=self.settings.register_proxy)
            self._cfmail_manager = DEFAULT_CFMAIL_MANAGER
            cfmail_module.CFMAIL_WAIT_ABORT_PREDICATE = self._should_abort_cfmail_wait
            cfmail_module.CFMAIL_WAIT_PROGRESS_CALLBACK = self._on_cfmail_wait_progress
            try:
                normalize_result = self._cfmail_provisioner.normalize_accounts_to_single_active_domain()
                self._cfmail_manager.reload_if_needed(force=True)
                removed_domains = list(normalize_result.get("removed_domains") or [])
                active_domain = str(normalize_result.get("active_domain") or "").strip()
                if removed_domains:
                    self._log(
                        "[zhuce6:register] [cfmail] normalized active domain state: "
                        f"active={active_domain or '-'} removed={', '.join(removed_domains)}"
                    )
            except Exception as exc:
                self._log(f"[zhuce6:register] [cfmail] normalize active domain state failed: {exc}")
            try:
                self._ensure_cfmail_active_domain_ready()
            except Exception as exc:
                self._log(f"[zhuce6:register] [cfmail] startup active-domain preflight failed: {exc}")
        if self.settings.backend == "cpa" and self.settings.cpa_runtime_reconcile_enabled:
            try:
                _maybe_reconcile_cpa_runtime(
                    pool_dir=self.settings.pool_dir,
                    management_base_url=self.settings.cpa_management_base_url,
                    enabled=True,
                    cooldown_seconds=self.settings.cpa_runtime_reconcile_cooldown_seconds,
                    restart_enabled=self.settings.cpa_runtime_reconcile_restart_enabled,
                    state_file=self.settings.pool_dir / "cpa_runtime_reconcile_state.json",
                    client=create_backend_client(self.settings),
                    management_key=self.settings.cpa_management_key,
                )
            except Exception as exc:
                self._log(f"[zhuce6:register] startup reconcile failed: {exc}")
        if self.settings.proxy_pool_configured:
            from core.proxy_pool import ProxyPool

            self._proxy_pool = ProxyPool.from_settings(self.settings)
            if self._proxy_pool is not None:
                self._proxy_pool.start()
        target_msg = f", target={self.settings.register_target_count}" if self.settings.register_target_count > 0 else ""
        self._log(
            f"[zhuce6:register] starting {num} threads, "
            f"providers={','.join(self._providers)}, "
            f"proxy={self.settings.register_proxy or 'none'}, "
            f"sleep={self.settings.register_sleep_min}-{self.settings.register_sleep_max}s"
            f"{target_msg}"
        )
        threading_module = _compat_main_attr("threading", threading)
        for i in range(num):
            provider = self._providers[i % len(self._providers)]
            t = threading_module.Thread(
                target=self._worker,
                args=(i + 1, provider),
                daemon=True,
                name=f"zhuce6-register-{i + 1}",
            )
            t.start()
            self._threads.append(t)
        # Solution B: start deferred retry worker thread
        pending_t = threading_module.Thread(
            target=self._pending_token_retry_worker,
            daemon=True,
            name="zhuce6-pending-token-retry",
        )
        pending_t.start()
        self._threads.append(pending_t)
        self._write_runtime_state()

    def stop(self) -> None:
        self._stop_event.set()
        for t in self._threads:
            t.join(timeout=2)
        self._threads.clear()
        try:
            import core.cfmail as cfmail_module

            cfmail_module.CFMAIL_WAIT_ABORT_PREDICATE = None
            cfmail_module.CFMAIL_WAIT_PROGRESS_CALLBACK = None
        except Exception:
            pass
        if self._proxy_pool is not None:
            self._proxy_pool.close()
            self._proxy_pool = None
        self._write_runtime_state()

    def _should_stop(self) -> bool:
        return self._stop_event.is_set() or self._target_reached.is_set()

    def _check_target(self) -> bool:
        """Return True if target reached and threads should stop."""
        if self.settings.register_target_count <= 0:
            return False
        with self._lock:
            if self._total_success >= self.settings.register_target_count:
                self._target_reached.set()
                return True
        return False

    def _cpa_api_root(self) -> str:
        parsed = urlsplit(self.settings.cpa_management_base_url)
        path = parsed.path or ""
        suffix = "/v0/management"
        if path.endswith(suffix):
            path = path[: -len(suffix)]
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")

    def _get_cpa_management_key(self) -> str | None:
        cached = self._cpa_management_key_cache
        if cached is not False:
            return str(cached or "") or None
        key = str(get_management_key() or "").strip() or None
        self._cpa_management_key_cache = key or None
        return key

    def _sync_cpa_from_success(self, result: dict[str, object], thread_id: int) -> tuple[bool, str, str]:
        """Persist a registered account to CPA immediately while keeping pool as backup."""

        pool_file_raw = str(result.get("pool_file") or "").strip()
        if not pool_file_raw:
            return False, "missing pool file", ""
        pool_file = Path(pool_file_raw)
        if not pool_file.is_file():
            return False, f"pool file missing: {pool_file.name}", ""

        sync_started_at = now_iso()
        key = self._get_cpa_management_key()
        if not key:
            update_token_record(
                pool_file,
                backup_written=True,
                cpa_sync_status="failed",
                last_cpa_sync_at=sync_started_at,
                last_cpa_sync_error="CPA management key unavailable",
            )
            return False, "CPA management key unavailable", ""
        try:
            from platforms.chatgpt.pool import load_token_record

            token_data = load_token_record(pool_file)
        except Exception as exc:
            return False, f"invalid pool file {pool_file.name}: {exc}", ""
        if not isinstance(token_data, dict) or not str(token_data.get("email") or "").strip():
            update_token_record(
                pool_file,
                backup_written=True,
                cpa_sync_status="failed",
                last_cpa_sync_at=sync_started_at,
                last_cpa_sync_error="missing email in pool record",
            )
            return False, f"missing email in {pool_file.name}", ""

        from platforms.chatgpt.cpa_upload import upload_to_cpa

        ok, message = upload_to_cpa(
            token_data,
            api_url=self._cpa_api_root(),
            api_key=key,
            proxy=None,
        )
        email = str(token_data.get("email") or pool_file.name).strip() or pool_file.name
        if ok:
            update_token_record(
                pool_file,
                health_status="good",
                backup_written=True,
                cpa_sync_status="synced",
                last_cpa_sync_at=sync_started_at,
                last_cpa_sync_error="",
            )
            return True, "", email
        else:
            update_token_record(
                pool_file,
                backup_written=True,
                cpa_sync_status="failed",
                last_cpa_sync_at=sync_started_at,
                last_cpa_sync_error=message,
            )
            return False, message, email

    # ── Solution B: Deferred retry queue ──────────────────────────

    def _enqueue_pending_token(self, result: dict[str, object], thread_id: int) -> None:
        """Save an add_phone_gate account for deferred token acquisition retry."""
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        deferred = metadata.get("deferred_credentials")
        if not isinstance(deferred, dict):
            return
        email = str(deferred.get("email") or "").strip()
        password = str(deferred.get("password") or "").strip()
        if not email or not password:
            return
        entry = {
            "email": email,
            "password": password,
            "mailbox_jwt": str(deferred.get("mailbox_jwt") or "").strip(),
            "mailbox_extra": dict(deferred.get("mailbox_extra") or {}),
            "created_at": time.time(),
            "retry_count": 0,
            "last_retry_at": 0.0,
        }
        with self._pending_token_lock:
            self._pending_token_queue.append(entry)
            self._pending_token_total_enqueued += 1
        self._log(
            f"[zhuce6:register] [thread-{thread_id}] 📥 deferred token retry enqueued: {email} "
            f"(queue_size={len(self._pending_token_queue)})"
        )

    def _pending_token_retry_worker(self) -> None:
        """Background thread that retries token acquisition for queued add_phone_gate accounts."""
        while not self._should_stop():
            if self._stop_event.wait(30):
                break
            batch: list[dict[str, Any]] = []
            now = time.time()
            with self._pending_token_lock:
                remaining: list[dict[str, Any]] = []
                for entry in self._pending_token_queue:
                    created_at = float(entry.get("created_at") or 0)
                    retry_count = int(entry.get("retry_count") or 0)
                    last_retry = float(entry.get("last_retry_at") or 0)
                    age = now - created_at
                    since_last = now - last_retry if last_retry > 0 else age
                    delay = self._pending_token_retry_delay_seconds * (retry_count + 1)
                    if retry_count >= self._pending_token_max_retries:
                        self._pending_token_total_failed += 1
                        self._log(
                            f"[zhuce6:register] [pending] ❌ exhausted retries for "
                            f"{entry.get('email')}, discarding"
                        )
                        continue
                    if since_last >= delay:
                        batch.append(entry)
                    else:
                        remaining.append(entry)
                self._pending_token_queue = remaining
            if not batch:
                continue
            for entry in batch:
                if self._should_stop():
                    break
                self._retry_pending_token(entry)

    def _retry_pending_token(self, entry: dict[str, Any]) -> None:
        """Attempt token acquisition for a single deferred account."""
        email = str(entry.get("email") or "").strip()
        password = str(entry.get("password") or "").strip()
        mailbox_jwt = str(entry.get("mailbox_jwt") or "").strip()
        mailbox_extra = dict(entry.get("mailbox_extra") or {})
        retry_count = int(entry.get("retry_count") or 0) + 1
        self._log(
            f"[zhuce6:register] [pending] 🔄 retrying token acquisition "
            f"for {email} (attempt {retry_count}/{self._pending_token_max_retries})"
        )
        try:
            from core.base_mailbox import MailboxAccount
            from core.cfmail import CfMailMailbox, DEFAULT_CFMAIL_MANAGER
            from platforms.chatgpt.plugin import MailboxEmailServiceAdapter
            from platforms.chatgpt.register import RegistrationEngine

            mailbox = CfMailMailbox(manager=DEFAULT_CFMAIL_MANAGER)
            adapter = MailboxEmailServiceAdapter(mailbox)
            # Reconstruct the mailbox account so _wait_for_mailbox_code can poll
            if mailbox_jwt and mailbox_extra:
                adapter._account = MailboxAccount(
                    email=email,
                    account_id=mailbox_jwt,
                    extra=dict(mailbox_extra),
                )
            engine = RegistrationEngine(
                email_service=adapter,
                proxy_url=self.settings.register_proxy,
            )
            engine.email = email
            engine.password = password
            token_info = engine._login_for_token()
            if token_info:
                self._log(f"[zhuce6:register] [pending] ✅ deferred token acquired for {email}")
                # Write to pool
                from platforms.chatgpt.pool import write_token_record
                token_data = {
                    "type": "codex",
                    "email": email,
                    "password": password,
                    "mail_provider": "cfmail",
                    "expired": str(token_info.get("expired") or ""),
                    "id_token": str(token_info.get("id_token") or ""),
                    "account_id": str(token_info.get("account_id") or ""),
                    "access_token": str(token_info.get("access_token") or ""),
                    "last_refresh": str(token_info.get("last_refresh") or ""),
                    "refresh_token": str(token_info.get("refresh_token") or ""),
                    "source": "deferred_retry",
                }
                pool_file = write_token_record(token_data, self.settings.pool_dir)
                sync_ok, sync_error, synced_email = self._sync_cpa_from_success(
                    {"pool_file": str(pool_file), "success": True, "stage": "deferred_retry"},
                    thread_id=0,
                )
                with self._lock:
                    if sync_ok:
                        self._total_success += 1
                        self._total_cpa_sync_success += 1
                        self._pending_token_total_success += 1
                    else:
                        self._total_failure += 1
                        self._total_cpa_sync_failure += 1
                        self._pending_token_total_failed += 1
                if sync_ok:
                    self._log(f"[zhuce6:register] [pending] ✅ CPA sync success: {synced_email or email}")
                else:
                    self._log(f"[zhuce6:register] [pending] ❌ failed [stage=cpa_sync]: {sync_error or 'unknown'}")
                return
            else:
                self._log(f"[zhuce6:register] [pending] ⏳ deferred retry failed for {email}")
        except Exception as exc:
            self._log(f"[zhuce6:register] [pending] ⚠️ deferred retry error for {email}: {exc}")
        # Re-enqueue with incremented retry count
        entry["retry_count"] = retry_count
        entry["last_retry_at"] = time.time()
        with self._pending_token_lock:
            self._pending_token_queue.append(entry)

    def _worker(self, thread_id: int, initial_provider: str) -> None:
        provider = initial_provider
        consecutive_failures = 0
        max_failures = self.settings.register_max_consecutive_failures

        while not self._should_stop():
            if provider == "cfmail" and self._cfmail_tracker is not None:
                self._cfmail_rotation_pause.wait()
            if self._wait_if_cfmail_add_phone_stopped(thread_id, provider):
                continue
            if self._wait_if_cfmail_wait_otp_stopped(thread_id, provider):
                continue
            if self._wait_if_cfmail_canary_pending(thread_id, provider):
                continue
            if provider == "cfmail" and self._cfmail_manager is not None:
                if self._check_cfmail_all_cooldown_rotation(thread_id):
                    continue
                if self._cfmail_all_accounts_in_cooldown():
                    self._stop_event.wait(10)
                    continue
            if self._wait_if_cfmail_flow_throttled(thread_id, provider):
                continue
            try:
                result: dict[str, object] = {}
                proxy_key = ""
                proxy_outcome: bool | None = False
                result_metadata: dict[str, object] = {}
                result_stage = "?"
                result_error = ""
                result_email = ""
                max_proxy_attempts = 2 if self._proxy_pool is not None else 1
                for proxy_attempt in range(1, max_proxy_attempts + 1):
                    proxy_lease = None
                    proxy_url = self.settings.register_proxy
                    proxy_key = proxy_url or ""
                    proxy_outcome = False
                    release_stage = "exception"
                    try:
                        if self._proxy_pool is not None:
                            proxy_lease = self._proxy_pool.acquire(timeout=5)
                            proxy_url = proxy_lease.proxy_url
                            proxy_key = proxy_lease.name or proxy_url or ""
                            self._log(
                                f"[zhuce6:register] [thread-{thread_id}] acquired proxy {proxy_lease.local_port} ({proxy_lease.name})"
                            )
                        self._log(f"[zhuce6:register] [thread-{thread_id}] attempting (provider={provider})")
                        result = _compat_main_attr("run_chatgpt_register_once", run_chatgpt_register_once)(
                            email=None,
                            password=None,
                            mail_provider=provider,
                            proxy=proxy_url,
                            write_pool=True,
                            pool_dir=self.settings.pool_dir,
                        )
                        proxy_outcome = self._classify_proxy_outcome(result)
                        result_metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
                        result_stage = str(result.get("stage") or "?").strip() or "?"
                        if bool(result.get("success")) and result_stage == "?":
                            result_stage = "completed"
                        result_error = str(result.get("error_message") or "").strip()
                        result_email = str(result.get("email") or "").strip()
                        release_stage = result_stage
                        should_retry_device_id = (
                            self._proxy_pool is not None
                            and proxy_attempt < max_proxy_attempts
                            and not bool(result.get("success"))
                            and result_stage == "device_id"
                        )
                        if should_retry_device_id:
                            self._log(
                                f"[zhuce6:register] [thread-{thread_id}] device_id failed on proxy {proxy_key}; "
                                "rotating proxy and retrying once"
                            )
                            continue
                        break
                    finally:
                        if proxy_lease is not None and self._proxy_pool is not None:
                            try:
                                self._proxy_pool.release(
                                    proxy_lease,
                                    success=proxy_outcome,
                                    stage=release_stage,
                                )
                            except Exception as exc:
                                self._log(f"[zhuce6:register] [thread-{thread_id}] proxy release failed: {exc}")
                success = bool(result.get("success"))
                should_break_after_iteration = False
                with self._lock:
                    self._total_attempts += 1
                if success:
                    sync_ok, sync_error, synced_email = self._sync_cpa_from_success(result, thread_id)
                    with self._lock:
                        if sync_ok:
                            self._total_success += 1
                            self._total_cpa_sync_success += 1
                            self._last_error = None
                            consecutive_failures = 0
                            self._record_attempt(
                                success=True,
                                stage=result_stage,
                                error_message="",
                                metadata=result_metadata,
                                proxy_key=proxy_key,
                                email=result_email or synced_email,
                            )
                            email = result_email or synced_email or "?"
                            self._log(f"[zhuce6:register] [thread-{thread_id}] \u2705 success: {email}")
                            self._log(f"[zhuce6:register] [thread-{thread_id}] ✅ CPA sync success: {email}")
                            if self._check_target():
                                self._log(
                                    f"[zhuce6:register] target reached ({self.settings.register_target_count}), stopping"
                                )
                                should_break_after_iteration = True
                        else:
                            self._total_failure += 1
                            self._total_cpa_sync_failure += 1
                            consecutive_failures += 1
                            err = sync_error or "CPA sync failed"
                            self._last_error = err
                            self._record_attempt(
                                success=False,
                                stage="cpa_sync",
                                error_message=err,
                                metadata=result_metadata,
                                proxy_key=proxy_key,
                                email=result_email or synced_email,
                            )
                            self._log(
                                f"[zhuce6:register] [thread-{thread_id}] \u274c failed ({consecutive_failures}/{max_failures}) "
                                f"[stage=cpa_sync]: {err}"
                            )
                else:
                    with self._lock:
                        self._total_failure += 1
                        consecutive_failures += 1
                        err = result_error or "unknown"
                        stage = result_stage
                        self._last_error = err
                        self._record_attempt(
                            success=False,
                            stage=stage,
                            error_message=err,
                            metadata=result_metadata,
                            proxy_key=proxy_key,
                            email=result_email,
                        )
                        self._log(f"[zhuce6:register] [thread-{thread_id}] \u274c failed ({consecutive_failures}/{max_failures}) [stage={stage}]: {err}")
                        for log_line in result.get("logs", []):
                            self._log(f"[zhuce6:register] [thread-{thread_id}]   \u21b3 {log_line}")
                # Solution B: enqueue add_phone_gate accounts for deferred retry
                if result_stage == "add_phone_gate":
                    self._enqueue_pending_token(result, thread_id)
                invalid_mailbox_rotation = self._force_rotate_cfmail_for_invalid_mailbox(
                    thread_id,
                    result,
                )
                canary_rotation = self._rotate_cfmail_for_failed_canary(thread_id, result)
                rotation_success = self._handle_cfmail_rotation(
                    thread_id=thread_id,
                    result=result,
                    proxy_key=proxy_key,
                )
                self._update_cfmail_add_phone_stoploss(result)
                self._update_cfmail_wait_otp_stoploss(result)
                self._update_cfmail_canary_after_result(thread_id=thread_id, result=result)
                self._update_cfmail_fresh_domain_budget(result)
                fresh_domain_rotation = self._rotate_cfmail_for_fresh_domain_budget(thread_id)
                if invalid_mailbox_rotation or canary_rotation or rotation_success or fresh_domain_rotation:
                    consecutive_failures = 0
                self._write_runtime_state()
                if should_break_after_iteration:
                    break
            except Exception as exc:
                with self._lock:
                    self._total_attempts += 1
                    self._total_failure += 1
                    self._last_error = str(exc)
                    self._record_attempt(
                        success=False,
                        stage="exception",
                        error_message=str(exc),
                        metadata={"mail_provider": provider},
                        proxy_key=proxy_key,
                        email="",
                    )
                consecutive_failures += 1
                self._log(f"[zhuce6:register] [thread-{thread_id}] \u274c exception ({consecutive_failures}/{max_failures}): {exc}")
                self._write_runtime_state()
            finally:
                self._release_cfmail_flow_slot(thread_id)

            # Fallback: switch provider after N consecutive failures
            if consecutive_failures >= max_failures and len(self._providers) > 1:
                old_provider = provider
                current_idx = self._providers.index(provider) if provider in self._providers else 0
                provider = self._providers[(current_idx + 1) % len(self._providers)]
                consecutive_failures = 0
                self._log(
                    f"[zhuce6:register] [thread-{thread_id}] [fallback] switching {old_provider} -> {provider}"
                )

            # Random sleep between attempts
            sleep_sec = random.randint(
                self.settings.register_sleep_min,
                max(self.settings.register_sleep_min, self.settings.register_sleep_max),
            )
            if self._stop_event.wait(sleep_sec) or self._target_reached.is_set():
                break

    def _cfmail_all_accounts_in_cooldown(self) -> bool:
        manager = self._cfmail_manager
        if manager is None:
            return False
        try:
            manager.reload_if_needed()
        except Exception:
            pass
        return manager.select_account() is None

    def _current_cfmail_active_domain(self) -> str:
        try:
            from core.cfmail import active_cfmail_domain

            manager = self._cfmail_manager
            config_path = getattr(manager, "config_path", None)
            return str(active_cfmail_domain(config_path)).strip().lower()
        except Exception:
            return ""

    def _should_abort_cfmail_wait(self, account: Any) -> bool:
        try:
            extra = account.extra if hasattr(account, "extra") and isinstance(account.extra, dict) else {}
            domain = str(extra.get("email_domain") or "").strip().lower()
            if not domain:
                email = str(getattr(account, "email", "") or "").strip().lower()
                if "@" in email:
                    domain = email.rsplit("@", 1)[-1].strip().lower()
            if not domain:
                return False
            stoploss = self._cfmail_wait_otp_stoploss_snapshot()
            if not bool(stoploss.get("in_cooldown")):
                return False
            if str(stoploss.get("active_domain") or "").strip().lower() != domain:
                return False
            wait_started_at = float(extra.get("otp_wait_started_at") or 0.0)
            triggered_at_raw = str(stoploss.get("last_triggered_at") or "").strip()
            if wait_started_at > 0.0 and triggered_at_raw:
                try:
                    triggered_at = datetime.fromisoformat(triggered_at_raw).timestamp()
                except Exception:
                    triggered_at = 0.0
                if triggered_at > 0.0 and wait_started_at < triggered_at:
                    return False
            return True
        except Exception:
            return False

    def _check_cfmail_all_cooldown_rotation(self, thread_id: int) -> bool:
        """When all cfmail accounts are in cooldown, proactively trigger domain rotation."""
        if self._cfmail_tracker is None or self._cfmail_provisioner is None:
            return False
        if not self._cfmail_all_accounts_in_cooldown():
            return False
        if not self._cfmail_rotation_lock.acquire(blocking=False):
            return False
        self._cfmail_rotation_pause.clear()
        try:
            self._log(
                f"[zhuce6:register] [thread-{thread_id}] [cfmail] all accounts in cooldown, "
                "forcing domain rotation to break deadlock"
            )
            provision_result = self._cfmail_provisioner.rotate_active_domain()
            if not provision_result.success:
                if provision_result.old_domain:
                    self._cfmail_tracker.mark_rotation_failed(
                        provision_result.old_domain,
                        provision_result.error,
                    )
                self._log(
                    f"[zhuce6:register] [thread-{thread_id}] [cfmail] deadlock rotation failed: "
                    f"{provision_result.error}"
                )
                return False
            self._cfmail_tracker.mark_rotation_completed(
                provision_result.old_domain,
                provision_result.new_domain,
            )
            self._reload_cfmail_manager_after_rotation()
            self._reset_cfmail_add_phone_stoploss(provision_result.new_domain)
            self._reset_cfmail_wait_otp_stoploss(provision_result.new_domain)
            self._reset_cfmail_fresh_domain_budget(provision_result.new_domain)
            self._arm_cfmail_canary(provision_result.new_domain)
            self._log(
                f"[zhuce6:register] [thread-{thread_id}] [cfmail] deadlock rotation completed: "
                f"{provision_result.old_domain} -> {provision_result.new_domain}"
            )
            return True
        finally:
            self._cfmail_rotation_pause.set()
            self._cfmail_rotation_lock.release()

    def _handle_cfmail_rotation(
        self,
        *,
        thread_id: int,
        result: dict[str, object],
        proxy_key: str,
    ) -> bool:
        if self._cfmail_tracker is None or self._cfmail_provisioner is None:
            return False
        metadata = result.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        if str(metadata.get("mail_provider") or result.get("mail_provider") or "").strip().lower() not in {"", "cfmail"}:
            return False
        from core.cfmail_domain_rotation import classify_domain_attempt

        attempt = classify_domain_attempt(result, proxy_key=proxy_key)
        if attempt is None:
            return False
        decision = self._cfmail_tracker.record(attempt)
        if attempt.backend_failure and not decision.should_rotate:
            self._log(
                f"[zhuce6:register] [thread-{thread_id}] [cfmail] backend unhealthy for domain={attempt.domain}; rotation skipped"
            )
            return False
        if not decision.should_rotate:
            return False
        if not self._cfmail_rotation_lock.acquire(blocking=False):
            self._log(f"[zhuce6:register] [thread-{thread_id}] [cfmail] rotation already in progress")
            return False
        self._cfmail_rotation_pause.clear()
        try:
            self._cfmail_tracker.mark_rotation_started(decision.domain, decision.reason)
            self._log(
                f"[zhuce6:register] [thread-{thread_id}] [cfmail] rotating domain {decision.domain} "
                f"(blacklist_failures={decision.blacklist_failures}, window={decision.window_size})"
            )
            provision_result = self._cfmail_provisioner.rotate_active_domain()
            if not provision_result.success:
                self._cfmail_tracker.mark_rotation_failed(decision.domain, provision_result.error)
                self._log(
                    f"[zhuce6:register] [thread-{thread_id}] [cfmail] rotation failed: {provision_result.error}"
                )
                return False
            self._cfmail_tracker.mark_rotation_completed(
                provision_result.old_domain,
                provision_result.new_domain,
            )
            self._reload_cfmail_manager_after_rotation()
            self._reset_cfmail_add_phone_stoploss(provision_result.new_domain)
            self._reset_cfmail_wait_otp_stoploss(provision_result.new_domain)
            self._reset_cfmail_fresh_domain_budget(provision_result.new_domain)
            self._arm_cfmail_canary(provision_result.new_domain)
            self._log(
                f"[zhuce6:register] [thread-{thread_id}] [cfmail] rotation completed: "
                f"{provision_result.old_domain} -> {provision_result.new_domain}"
            )
            return True
        finally:
            self._cfmail_rotation_pause.set()
            self._cfmail_rotation_lock.release()

    def _classify_proxy_outcome(self, result: dict[str, object]) -> bool | None:
        if bool(result.get("success")):
            return True
        stage = str(result.get("stage") or "").strip().lower()
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        code = str(metadata.get("create_account_error_code") or "").strip().lower()
        post_create_gate = str(metadata.get("post_create_gate") or "").strip().lower()
        if stage == "create_account" and code in {"registration_disallowed", "unsupported_email"}:
            return None
        if stage == "add_phone_gate" or post_create_gate == "add_phone":
            return None
        if stage in {"mailbox", "device_id", "password", "token_acquisition"}:
            return False
        return False

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            alive = sum(1 for t in self._threads if t.is_alive())
            target = self.settings.register_target_count
            cfmail_rotation = self._cfmail_tracker.snapshot() if self._cfmail_tracker is not None else None
            recent_attempts = list(self._recent_attempts)
            add_phone_stoploss = self._cfmail_add_phone_stoploss_snapshot()
            wait_otp_stoploss = self._cfmail_wait_otp_stoploss_snapshot()
            cfmail_canary = self._cfmail_canary_snapshot()
            cfmail_fresh_domain_budget = self._cfmail_fresh_domain_budget_snapshot()
            active_domain = self._infer_active_domain(recent_attempts, cfmail_rotation, add_phone_stoploss)
            active_domain_attempts = self._active_domain_attempts(recent_attempts, active_domain)
            active_failure_by_stage, active_failure_signals = self._failure_counts_from_attempts(active_domain_attempts)
            return {
                "name": "register",
                "status": "running" if alive > 0 else ("pending" if self._total_attempts == 0 else "stopped"),
                "threads_alive": alive,
                "threads_total": len(self._threads),
                "total_attempts": self._total_attempts,
                "total_success": self._total_success,
                "total_success_registered": self._total_success,
                "total_cpa_sync_success": self._total_cpa_sync_success,
                "total_cpa_sync_failure": self._total_cpa_sync_failure,
                "total_failure": self._total_failure,
                "success_rate": round(self._total_success / max(self._total_attempts, 1) * 100, 1),
                "registered_success_rate": round(self._total_success / max(self._total_attempts, 1) * 100, 1),
                "cpa_sync_success_rate": round(self._total_cpa_sync_success / max(self._total_attempts, 1) * 100, 1),
                "target_count": target if target > 0 else None,
                "target_reached": self._target_reached.is_set(),
                "last_error": self._last_error,
                "proxy": self.settings.register_proxy,
                "proxy_pool_enabled": self._proxy_pool is not None,
                "mail_provider": self.settings.register_mail_provider,
                "interval_seconds": self.settings.register_interval,
                "run_count": self._total_attempts,
                "success_count": self._total_success,
                "failure_count": self._total_failure,
                "is_running": alive > 0,
                "last_started_at": datetime.fromtimestamp(self._started_at).isoformat(timespec="seconds") if self._started_at else None,
                "last_finished_at": None,
                "last_duration_seconds": None,
                "next_run_at": None,
                "failure_by_stage": dict(sorted(self._failure_by_stage.items(), key=lambda item: (-item[1], item[0]))),
                "failure_signals": dict(sorted(self._failure_signals.items(), key=lambda item: (-item[1], item[0]))),
                "recent_failure_hotspots": self._recent_failure_hotspots(),
                "recent_attempts": recent_attempts,
                "active_domain_recent_attempts": active_domain_attempts,
                "active_domain_failure_by_stage": active_failure_by_stage,
                "active_domain_failure_signals": active_failure_signals,
                "active_domain_recent_failure_hotspots": self._recent_failure_hotspots_from_attempts(active_domain_attempts),
                "cfmail_rotation": cfmail_rotation,
                "cfmail_add_phone_stoploss": add_phone_stoploss,
                "cfmail_wait_otp_stoploss": wait_otp_stoploss,
                "cfmail_canary": cfmail_canary,
                "cfmail_fresh_domain_budget": cfmail_fresh_domain_budget,
                "pending_token_queue": {
                    "queue_size": len(self._pending_token_queue),
                    "total_enqueued": self._pending_token_total_enqueued,
                    "total_success": self._pending_token_total_success,
                    "total_failed": self._pending_token_total_failed,
                    "retry_delay_seconds": self._pending_token_retry_delay_seconds,
                    "max_retries": self._pending_token_max_retries,
                },
            }


class RegistrationBurstScheduler:
    """Run registration in timed batches and expose scheduler state via runtime_state.json."""

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._active_loop: RegistrationLoop | None = None
        self._active_started_at: float | None = None
        self._next_run_at_ts: float | None = None
        self._run_count = 0
        self._total_attempts = 0
        self._total_success = 0
        self._total_cpa_sync_success = 0
        self._total_cpa_sync_failure = 0
        self._total_failure = 0
        self._last_error: str | None = None
        self._recent_attempts: deque[dict[str, object]] = deque(maxlen=80)
        self._failure_by_stage: dict[str, int] = {}
        self._failure_signals: dict[str, int] = {}
        self._last_batch_started_at: str | None = None
        self._last_batch_finished_at: str | None = None
        self._last_batch_duration_seconds: float | None = None
        self._last_cfmail_add_phone_stoploss: dict[str, object] | None = None
        self._last_cfmail_wait_otp_stoploss: dict[str, object] | None = None
        self._last_proxy_pool_snapshot: dict[str, object] = {
            "configured": bool(self.settings.proxy_pool_configured),
            "enabled": False,
            "snapshot_error": None,
            "node_count": 0,
            "in_use_count": 0,
            "disabled_count": 0,
            "nodes": [],
        }
        self._logger = self._setup_logger()

    def _setup_logger(self) -> Any:
        from logging.handlers import RotatingFileHandler

        logger = logging.getLogger("zhuce6.register")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if not logger.handlers:
            console = logging.StreamHandler()
            console.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(console)
            if self.settings.register_log_file:
                fh = RotatingFileHandler(
                    self.settings.register_log_file,
                    maxBytes=2 * 1024 * 1024,
                    backupCount=5,
                    encoding="utf-8",
                )
                fh.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
                logger.addHandler(fh)
        return logger

    def _log(self, msg: str) -> None:
        self._logger.info(msg)

    def _merge_counts(self, target: dict[str, int], incoming: dict[str, object] | None) -> None:
        if not isinstance(incoming, dict):
            return
        for key, value in incoming.items():
            try:
                inc = int(value or 0)
            except Exception:
                continue
            target[str(key)] = target.get(str(key), 0) + inc

    def _write_runtime_state(self) -> None:
        state_file = Path(self.settings.runtime_state_file)
        try:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "register_snapshot": self.snapshot(),
                "proxy_pool": self._current_proxy_pool_snapshot(),
            }
            tmp_file = state_file.with_name(
                f"{state_file.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            tmp_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp_file.replace(state_file)
        except Exception as exc:
            self._log(f"[zhuce6:register] burst runtime state write failed: {exc}")

    def _current_proxy_pool_snapshot(self) -> dict[str, object]:
        with self._lock:
            active_loop = self._active_loop
            last_snapshot = dict(self._last_proxy_pool_snapshot)
        if active_loop is not None:
            try:
                return active_loop._proxy_pool_snapshot()
            except Exception as exc:
                last_snapshot["snapshot_error"] = str(exc)
                return last_snapshot
        return last_snapshot

    def _absorb_batch_snapshot(self, snapshot: dict[str, object], *, duration_seconds: float) -> None:
        with self._lock:
            self._run_count += 1
            self._total_attempts += int(snapshot.get("total_attempts") or 0)
            self._total_success += int(snapshot.get("total_success") or 0)
            self._total_cpa_sync_success += int(snapshot.get("total_cpa_sync_success") or 0)
            self._total_cpa_sync_failure += int(snapshot.get("total_cpa_sync_failure") or 0)
            self._total_failure += int(snapshot.get("total_failure") or 0)
            self._last_error = str(snapshot.get("last_error") or "").strip() or None
            self._last_batch_started_at = snapshot.get("last_started_at") if isinstance(snapshot.get("last_started_at"), str) else None
            self._last_batch_finished_at = datetime.now().isoformat(timespec="seconds")
            self._last_batch_duration_seconds = round(duration_seconds, 3)
            self._merge_counts(self._failure_by_stage, snapshot.get("failure_by_stage") if isinstance(snapshot.get("failure_by_stage"), dict) else None)
            self._merge_counts(self._failure_signals, snapshot.get("failure_signals") if isinstance(snapshot.get("failure_signals"), dict) else None)
            attempts = snapshot.get("recent_attempts")
            if isinstance(attempts, list):
                for item in attempts:
                    if isinstance(item, dict):
                        self._recent_attempts.append(item)
            if isinstance(snapshot.get("cfmail_add_phone_stoploss"), dict):
                self._last_cfmail_add_phone_stoploss = dict(snapshot.get("cfmail_add_phone_stoploss") or {})
            if isinstance(snapshot.get("cfmail_wait_otp_stoploss"), dict):
                self._last_cfmail_wait_otp_stoploss = dict(snapshot.get("cfmail_wait_otp_stoploss") or {})

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            active_loop = self._active_loop
            next_run_at_ts = self._next_run_at_ts
            total_attempts = self._total_attempts
            total_success = self._total_success
            total_cpa_sync_success = self._total_cpa_sync_success
            total_cpa_sync_failure = self._total_cpa_sync_failure
            total_failure = self._total_failure
            run_count = self._run_count
            failure_by_stage = dict(sorted(self._failure_by_stage.items(), key=lambda item: (-item[1], item[0])))
            failure_signals = dict(sorted(self._failure_signals.items(), key=lambda item: (-item[1], item[0])))
            recent_attempts = list(self._recent_attempts)
            last_error = self._last_error
            last_batch_started_at = self._last_batch_started_at
            last_batch_finished_at = self._last_batch_finished_at
            last_batch_duration_seconds = self._last_batch_duration_seconds
            add_phone_stoploss = dict(self._last_cfmail_add_phone_stoploss or {})
            wait_otp_stoploss = dict(self._last_cfmail_wait_otp_stoploss or {})
        if active_loop is not None:
            current = dict(active_loop.snapshot())
            current.update(
                {
                    "scheduler_mode": "burst",
                    "batch_threads": self.settings.register_batch_threads,
                    "batch_target_count": self.settings.register_batch_target_count,
                    "batch_interval_seconds": self.settings.register_batch_interval_seconds,
                    "run_count": run_count,
                    "next_run_at": None,
                }
            )
            return current
        status = "scheduled" if next_run_at_ts and not self._stop_event.is_set() else ("stopped" if run_count > 0 or self._stop_event.is_set() else "pending")
        counts: dict[tuple[str, str], int] = {}
        for item in recent_attempts:
            if item.get("success"):
                continue
            stage = str(item.get("stage") or "?").strip() or "?"
            signal = str(item.get("signal") or "").strip()
            key = (signal or stage, stage)
            counts[key] = counts.get(key, 0) + 1
        recent_failure_hotspots = [
            {"key": key, "stage": stage, "count": count}
            for (key, stage), count in sorted(
                counts.items(),
                key=lambda kv: (-kv[1], kv[0][0], kv[0][1]),
            )[:5]
        ]
        return {
            "name": "register",
            "status": status,
            "scheduler_mode": "burst",
            "threads_alive": 0,
            "threads_total": self.settings.register_batch_threads,
            "total_attempts": total_attempts,
            "total_success": total_success,
            "total_success_registered": total_success,
            "total_cpa_sync_success": total_cpa_sync_success,
            "total_cpa_sync_failure": total_cpa_sync_failure,
            "total_failure": total_failure,
            "success_rate": round(total_success / max(total_attempts, 1) * 100, 1),
            "registered_success_rate": round(total_success / max(total_attempts, 1) * 100, 1),
            "cpa_sync_success_rate": round(total_cpa_sync_success / max(total_attempts, 1) * 100, 1),
            "target_count": self.settings.register_batch_target_count,
            "target_reached": False,
            "last_error": last_error,
            "proxy": self.settings.register_proxy,
            "proxy_pool_enabled": bool(self.settings.proxy_pool_configured),
            "mail_provider": self.settings.register_mail_provider,
            "interval_seconds": self.settings.register_interval,
            "run_count": run_count,
            "success_count": total_success,
            "failure_count": total_failure,
            "is_running": False,
            "last_started_at": last_batch_started_at,
            "last_finished_at": last_batch_finished_at,
            "last_duration_seconds": last_batch_duration_seconds,
            "next_run_at": datetime.fromtimestamp(next_run_at_ts).isoformat(timespec="seconds") if next_run_at_ts else None,
            "failure_by_stage": failure_by_stage,
            "failure_signals": failure_signals,
            "recent_failure_hotspots": recent_failure_hotspots,
            "recent_attempts": recent_attempts,
            "active_domain_recent_attempts": [],
            "active_domain_failure_by_stage": {},
            "active_domain_failure_signals": {},
            "active_domain_recent_failure_hotspots": [],
            "cfmail_rotation": None,
            "cfmail_add_phone_stoploss": add_phone_stoploss,
            "cfmail_wait_otp_stoploss": wait_otp_stoploss,
            "batch_threads": self.settings.register_batch_threads,
            "batch_target_count": self.settings.register_batch_target_count,
            "batch_interval_seconds": self.settings.register_batch_interval_seconds,
        }

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            active_loop = self._active_loop
        if active_loop is not None:
            active_loop.stop()
        self._write_runtime_state()

    def run(self) -> None:
        self._next_run_at_ts = time.time()
        self._write_runtime_state()
        while not self._stop_event.is_set():
            now = time.time()
            next_run_at_ts = self._next_run_at_ts or now
            if now < next_run_at_ts:
                self._write_runtime_state()
                self._stop_event.wait(min(max(next_run_at_ts - now, 1), 5))
                continue

            batch_settings = replace(
                self.settings,
                register_enabled=True,
                register_threads=self.settings.register_batch_threads,
                register_target_count=self.settings.register_batch_target_count,
            )
            loop = _compat_main_attr("RegistrationLoop", RegistrationLoop)(batch_settings)
            started_at = time.time()
            with self._lock:
                self._active_loop = loop
                self._active_started_at = started_at
            self._write_runtime_state()
            loop.start()
            try:
                while not self._stop_event.is_set():
                    snapshot = loop.snapshot()
                    if int(snapshot.get("threads_alive") or 0) <= 0:
                        break
                    self._write_runtime_state()
                    self._stop_event.wait(1)
            finally:
                loop.stop()
                batch_snapshot = loop.snapshot()
                with self._lock:
                    self._active_loop = None
                    self._active_started_at = None
                    proxy_snapshot_fn = getattr(loop, "_proxy_pool_snapshot", None)
                    if callable(proxy_snapshot_fn):
                        self._last_proxy_pool_snapshot = proxy_snapshot_fn()
                self._absorb_batch_snapshot(batch_snapshot, duration_seconds=time.time() - started_at)
                self._next_run_at_ts = started_at + self.settings.register_batch_interval_seconds
                self._write_runtime_state()
                if self._stop_event.is_set():
                    break

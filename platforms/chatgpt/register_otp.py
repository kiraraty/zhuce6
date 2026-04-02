"""OTP helpers for ChatGPT registration."""

from __future__ import annotations

import json
import time
from typing import Any

from .constants import OTP_CODE_PATTERN, OPENAI_API_ENDPOINTS


def _mailbox_context(self) -> tuple[Any | None, Any | None]:
    mailbox = getattr(self.email_service, "mailbox", None)
    account = getattr(self.email_service, "_account", None)
    if mailbox is None or account is None:
        return None, None
    return mailbox, account

def _capture_mailbox_ids(self) -> set[str]:
    mailbox, account = self._mailbox_context()
    if mailbox is None or account is None:
        return set()
    try:
        return set(mailbox.get_current_ids(account) or set())
    except Exception as exc:
        self._log(f"mailbox snapshot failed: {exc}")
        return set()

def _wait_for_mailbox_code(
    self,
    *,
    before_ids: set[str] | None = None,
    timeout: int = 180,
    keyword: str = "",
    not_before_timestamp: float | None = None,
) -> str:
    mailbox, account = self._mailbox_context()
    if mailbox is None or account is None:
        return ""
    try:
        wait_callable = getattr(mailbox, "wait_for_code")
        try:
            result = wait_callable(
                account,
                keyword=keyword,
                timeout=timeout,
                before_ids=before_ids,
                not_before_timestamp=not_before_timestamp,
            )
        except TypeError:
            result = wait_callable(
                account,
                keyword=keyword,
                timeout=timeout,
                before_ids=before_ids,
            )
        return str(result or "").strip()
    except Exception as exc:
        self._log(f"mailbox wait_for_code failed: {exc}")
        return ""

def _get_verification_code(self) -> str | None:
    if not self.email:
        return None
    self._last_otp_wait_failure_reason = ""
    self._last_otp_wait_diagnostics = {}
    try:
        mailbox, account = self._mailbox_context()
        if mailbox is not None and account is not None:
            started_at = time.time()
            baseline_ids = set(self._signup_otp_before_ids or set())
            self._log(
                "waiting for verification code via mailbox: "
                f"timeout={self._otp_wait_timeout_seconds}s baseline_ids={len(baseline_ids)}"
            )
            code = self._wait_for_mailbox_code(
                before_ids=baseline_ids,
                timeout=self._otp_wait_timeout_seconds,
                keyword="openai",
            )
            diagnostics = dict(getattr(mailbox, "last_wait_diagnostics", {}) or {})
            first_seen_at = diagnostics.get("first_message_seen_at")
            matched_at = diagnostics.get("matched_message_at")
            poll_count = diagnostics.get("poll_count") or 0
            message_scan_count = diagnostics.get("message_scan_count") or 0
            first_seen_delta = (
                round(float(first_seen_at) - float(self._otp_sent_at or started_at), 2)
                if first_seen_at is not None and self._otp_sent_at is not None
                else None
            )
            matched_delta = (
                round(float(matched_at) - float(self._otp_sent_at or started_at), 2)
                if matched_at is not None and self._otp_sent_at is not None
                else None
            )
            self._last_otp_wait_diagnostics = {
                "otp_mailbox_poll_count": int(poll_count),
                "otp_mailbox_message_scan_count": int(message_scan_count),
                "otp_mailbox_first_seen_after_seconds": first_seen_delta,
                "otp_mailbox_matched_after_seconds": matched_delta,
            }
            if diagnostics.get("aborted"):
                self._last_otp_wait_diagnostics["otp_mailbox_aborted"] = True
                self._last_otp_wait_diagnostics["otp_mailbox_abort_reason"] = str(
                    diagnostics.get("abort_reason") or ""
                ).strip()
            self._log(
                "otp mailbox diagnostics: "
                f"polls={poll_count} scanned={message_scan_count} "
                f"first_seen_after={first_seen_delta if first_seen_delta is not None else '-'}s "
                f"matched_after={matched_delta if matched_delta is not None else '-'}s"
            )
            self._signup_otp_before_ids = set()
            if code:
                self._log(f"verification code received: {code}")
                return code
            if diagnostics.get("aborted"):
                self._last_otp_wait_failure_reason = "mailbox_aborted_rotation"
                self._log("verification code wait aborted due to cfmail rotation")
                return None
            if message_scan_count <= 0:
                self._last_otp_wait_failure_reason = "mailbox_timeout_no_message"
            else:
                self._last_otp_wait_failure_reason = "mailbox_timeout_no_match"
            self._log(
                "verification code timed out "
                f"after {round(time.time() - started_at, 2)}s"
            )
            return None
        email_id = (self.email_info or {}).get("service_id")
        code = self.email_service.get_verification_code(
            email=self.email,
            email_id=email_id,
            timeout=self._otp_wait_timeout_seconds,
            pattern=OTP_CODE_PATTERN,
            otp_sent_at=self._otp_sent_at,
        )
        if code:
            self._log(f"verification code received: {code}")
            return code
        self._log(f"verification code timed out after {self._otp_wait_timeout_seconds}s")
        return None
    except Exception as exc:
        self._log(f"get_verification_code failed: {exc}")
        return None

def _validate_verification_code(self, code: str) -> bool:
    if self.session is None:
        return False
    try:
        response, session = self._session_request(
            session=self.session,
            method="POST",
            url=OPENAI_API_ENDPOINTS["validate_otp"],
            label="validate otp",
            refresh_session=self._refresh_registration_session,
            headers={
                "referer": "https://auth.openai.com/email-verification",
                "accept": "application/json",
                "content-type": "application/json",
            },
            data=json.dumps({"code": code}),
        )
        self.session = session
        self._log(f"validate otp status: {response.status_code}")
        return response.status_code == 200
    except Exception as exc:
        self._log(f"validate_verification_code failed: {exc}")
        return False

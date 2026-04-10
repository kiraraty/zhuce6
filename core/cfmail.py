"""cfmail integration for zhuce6."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import email.header
import re
import random
import secrets
import threading
import time
from typing import Any

_EMAIL_WORDS = [
    "blue", "red", "green", "silver", "gold", "dark", "bright", "cool",
    "sky", "star", "sun", "moon", "cloud", "rain", "snow", "wind",
    "ocean", "river", "lake", "forest", "mountain", "valley", "field",
    "wolf", "fox", "hawk", "eagle", "lion", "tiger", "bear", "deer",
    "swift", "brave", "calm", "wild", "free", "true", "bold", "wise",
    "code", "dev", "tech", "data", "pixel", "byte", "node", "stack",
    "north", "south", "east", "west", "prime", "alpha", "beta", "nova",
    "fire", "ice", "stone", "iron", "steel", "spark", "flash", "storm",
    "amber", "jade", "coral", "pearl", "ruby", "onyx", "opal", "zinc",
    "maple", "cedar", "birch", "aspen", "lotus", "ivy", "fern", "sage",
    "echo", "drift", "haze", "mist", "dusk", "dawn", "glow", "vibe",
    "jazz", "riff", "surf", "reef", "cove", "mesa", "ridge", "glen",
    "max", "leo", "kai", "zoe", "sam", "eli", "mia", "ace",
    "link", "page", "mark", "lane", "reed", "blake", "quinn", "logan",
    "atlas", "orion", "lyra", "cleo", "felix", "hugo", "nina", "vera",
    "neon", "retro", "cyber", "vapor", "sonic", "turbo", "hyper", "mega",
]

from curl_cffi import requests as cffi_requests

from .base_mailbox import BaseMailbox, MailboxAccount
from .paths import resolve_cfmail_config_path

DEFAULT_CFMAIL_CONFIG_PATH = resolve_cfmail_config_path()
DEFAULT_CFMAIL_FAIL_THRESHOLD = 3
DEFAULT_CFMAIL_COOLDOWN_SECONDS = 1800
DEFAULT_CFMAIL_REQUEST_ATTEMPTS = 3
DEFAULT_CFMAIL_RETRY_BASE_DELAY_SECONDS = 1.0
DEFAULT_CFMAIL_MAIL_LIST_LIMIT = 30
DEFAULT_CFMAIL_WAIT_POLL_INTERVAL_SECONDS = 3
CFMAIL_RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
CFMAIL_WAIT_ABORT_PREDICATE = None
CFMAIL_WAIT_PROGRESS_CALLBACK = None


@dataclass(frozen=True)
class CfmailAccount:
    name: str
    worker_domain: str
    email_domain: str
    admin_password: str


def _normalize_host(value: str) -> str:
    normalized = str(value or "").strip()
    if normalized.startswith("https://"):
        normalized = normalized[len("https://") :]
    elif normalized.startswith("http://"):
        normalized = normalized[len("http://") :]
    return normalized.strip().strip("/")


def load_cfmail_accounts_from_file(config_path: str | Path, *, silent: bool = False) -> list[dict[str, Any]]:
    path = Path(str(config_path or "").strip())
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        if silent:
            return []
        raise

    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("accounts"), list):
        return data["accounts"]
    return []


def _normalize_cfmail_account(raw: dict[str, Any]) -> CfmailAccount | None:
    if not isinstance(raw, dict):
        return None
    if not raw.get("enabled", True):
        return None
    name = str(raw.get("name") or "").strip()
    worker_domain = _normalize_host(raw.get("worker_domain") or raw.get("WORKER_DOMAIN") or "")
    email_domain = _normalize_host(raw.get("email_domain") or raw.get("EMAIL_DOMAIN") or "")
    admin_password = str(raw.get("admin_password") or raw.get("ADMIN_PASSWORD") or "").strip()
    if not name or not worker_domain or not email_domain or not admin_password:
        return None
    return CfmailAccount(
        name=name,
        worker_domain=worker_domain,
        email_domain=email_domain,
        admin_password=admin_password,
    )


def build_cfmail_accounts(raw_accounts: list[dict[str, Any]]) -> list[CfmailAccount]:
    accounts: list[CfmailAccount] = []
    seen_names: set[str] = set()
    for raw in raw_accounts:
        account = _normalize_cfmail_account(raw)
        if not account:
            continue
        key = account.name.lower()
        if key in seen_names:
            continue
        seen_names.add(key)
        accounts.append(account)
    return accounts


def enabled_cfmail_accounts(config_path: str | Path | None = None) -> list[CfmailAccount]:
    return build_cfmail_accounts(load_cfmail_accounts_from_file(config_path or DEFAULT_CFMAIL_CONFIG_PATH, silent=True))


def active_cfmail_domain(config_path: str | Path | None = None) -> str:
    accounts = enabled_cfmail_accounts(config_path)
    if not accounts:
        return ""
    return str(accounts[0].email_domain or "").strip().lower()


def cfmail_headers(*, jwt: str = "", use_json: bool = False) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if use_json:
        headers["Content-Type"] = "application/json"
    if jwt:
        headers["Authorization"] = f"Bearer {jwt}"
    return headers


def _is_transient_cfmail_exception(exc: Exception) -> bool:
    message = str(exc or "").lower()
    markers = (
        "connection timed out",
        "connection closed abruptly",
        "connection reset",
        "connection refused",
        "tls connect error",
        "recv failure",
        "send failure",
        "http/2 stream",
        "operation timed out",
        "curl: (7)",
        "curl: (28)",
        "curl: (35)",
        "curl: (52)",
        "curl: (55)",
        "curl: (56)",
    )
    return any(marker in message for marker in markers)


def _response_body_snippet(response: Any, limit: int = 240) -> str:
    try:
        if response is None:
            return ""
        text = str(getattr(response, "text", "") or "").strip()
        if text:
            return " ".join(text.split())[:limit]
        if getattr(response, "content", None):
            payload = response.json()
            return " ".join(json.dumps(payload, ensure_ascii=False).split())[:limit]
    except Exception:
        return ""
    return ""


def _message_timestamp_seconds(message: dict[str, Any]) -> float | None:
    raw = message.get("createdAt")
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


class CfmailAccountManager:
    def __init__(
        self,
        config_path: str | Path | None = None,
        *,
        profile_mode: str = "auto",
        hot_reload_enabled: bool = True,
        fail_threshold: int = DEFAULT_CFMAIL_FAIL_THRESHOLD,
        cooldown_seconds: int = DEFAULT_CFMAIL_COOLDOWN_SECONDS,
    ) -> None:
        self.config_path = Path(config_path or DEFAULT_CFMAIL_CONFIG_PATH)
        self.profile_mode = str(profile_mode or "auto").strip() or "auto"
        self.hot_reload_enabled = hot_reload_enabled
        self.fail_threshold = max(1, int(fail_threshold))
        self.cooldown_seconds = max(0, int(cooldown_seconds))
        self._account_lock = threading.Lock()
        self._reload_lock = threading.Lock()
        self._failure_lock = threading.Lock()
        self._account_index = 0
        self.accounts = build_cfmail_accounts(
            load_cfmail_accounts_from_file(self.config_path, silent=True)
        )
        self.config_mtime = self._current_mtime()
        self.failure_state: dict[str, dict[str, Any]] = {}

    def _current_mtime(self) -> float | None:
        try:
            return self.config_path.stat().st_mtime
        except OSError:
            return None

    def account_names(self, accounts: list[CfmailAccount] | None = None) -> str:
        items = accounts if accounts is not None else self.accounts
        return ", ".join(account.name for account in items) if items else "无"

    def set_accounts(self, accounts: list[CfmailAccount]) -> None:
        with self._account_lock:
            self.accounts = accounts
            self._account_index = 0
        self.prune_failure_state(accounts)

    def prune_failure_state(self, accounts: list[CfmailAccount] | None = None) -> None:
        valid_keys = {account.name.lower() for account in (accounts if accounts is not None else self.accounts)}
        with self._failure_lock:
            for key in list(self.failure_state.keys()):
                if key not in valid_keys:
                    self.failure_state.pop(key, None)

    def skip_remaining_seconds(self, account_name: str) -> int:
        key = str(account_name or "").strip().lower()
        if not key:
            return 0
        with self._failure_lock:
            cooldown_until = float((self.failure_state.get(key) or {}).get("cooldown_until") or 0)
        return max(0, int(cooldown_until - time.time()))

    def record_success(self, account_name: str) -> None:
        key = str(account_name or "").strip().lower()
        if not key:
            return
        with self._failure_lock:
            state = self.failure_state.setdefault(key, {"name": account_name})
            state["name"] = account_name
            state["consecutive_failures"] = 0
            state["cooldown_until"] = 0
            state["last_error"] = ""
            state["last_success_at"] = time.time()

    def record_failure(self, account_name: str, reason: str = "") -> None:
        key = str(account_name or "").strip().lower()
        if not key:
            return
        now = time.time()
        with self._failure_lock:
            state = self.failure_state.setdefault(key, {"name": account_name})
            state["name"] = account_name
            state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
            state["last_error"] = str(reason or "").strip()[:300]
            state["last_failed_at"] = now
            if state["consecutive_failures"] >= self.fail_threshold:
                state["cooldown_until"] = max(float(state.get("cooldown_until") or 0), now + self.cooldown_seconds)
                state["consecutive_failures"] = 0

    def reload_if_needed(self, force: bool = False) -> bool:
        if not self.hot_reload_enabled:
            return False
        mtime = self._current_mtime()
        if mtime is None:
            return False
        with self._reload_lock:
            if not force and self.config_mtime == mtime:
                return False
            accounts = build_cfmail_accounts(load_cfmail_accounts_from_file(self.config_path, silent=True))
            if not accounts:
                self.config_mtime = mtime
                return False
            self.set_accounts(accounts)
            self.config_mtime = mtime
            return True

    def select_account(self, profile_name: str | None = None) -> CfmailAccount | None:
        selected_name = str(profile_name or self.profile_mode or "auto").strip() or "auto"
        accounts = self.accounts
        if not accounts:
            return None

        if selected_name.lower() != "auto":
            selected_key = selected_name.lower()
            for account in accounts:
                if account.name.lower() == selected_key:
                    return account
            return None

        with self._account_lock:
            start_index = self._account_index % len(accounts)
            for offset in range(len(accounts)):
                index = (start_index + offset) % len(accounts)
                account = accounts[index]
                if self.skip_remaining_seconds(account.name) > 0:
                    continue
                self._account_index = (index + 1) % len(accounts)
                return account
        return None


class CfMailMailbox(BaseMailbox):
    def __init__(
        self,
        *,
        manager: CfmailAccountManager | None = None,
        profile_name: str = "auto",
        proxy: str | None = None,
    ) -> None:
        self.manager = manager or DEFAULT_CFMAIL_MANAGER
        self.profile_name = str(profile_name or "auto").strip() or "auto"
        # Cfmail worker inbox APIs are public web endpoints and do not benefit from
        # the shared register SOCKS5 path. In live traffic, routing these mailbox
        # operations through register proxies causes repeated
        # `curl: (97) cannot complete SOCKS5 connection` failures against the
        # worker domain. Keep mailbox create/list/wait on direct egress so the
        # register proxy pool only carries the OpenAI auth chain.
        del proxy
        self.proxies = None
        self.last_wait_diagnostics: dict[str, Any] = {}

    def _mail_list_limit(self) -> int:
        raw = str(os.getenv("ZHUCE6_CFMAIL_MAIL_LIST_LIMIT", str(DEFAULT_CFMAIL_MAIL_LIST_LIMIT)) or "").strip()
        try:
            value = int(raw)
        except Exception:
            value = DEFAULT_CFMAIL_MAIL_LIST_LIMIT
        return max(10, min(value, 100))

    def _request_with_retry(
        self,
        *,
        method: str,
        url: str,
        retry_label: str,
        max_attempts: int = DEFAULT_CFMAIL_REQUEST_ATTEMPTS,
        retry_delay: float = DEFAULT_CFMAIL_RETRY_BASE_DELAY_SECONDS,
        **kwargs: Any,
    ) -> Any:
        last_exc: Exception | None = None
        last_response: Any | None = None
        requester = getattr(cffi_requests, method.lower())
        for attempt in range(1, max_attempts + 1):
            try:
                response = requester(url, **kwargs)
                last_response = response
            except Exception as exc:
                last_exc = exc
                if attempt < max_attempts and _is_transient_cfmail_exception(exc):
                    time.sleep(retry_delay * attempt)
                    continue
                raise
            if response.status_code in CFMAIL_RETRYABLE_STATUS_CODES and attempt < max_attempts:
                time.sleep(retry_delay * attempt)
                continue
            return response
        if last_exc is not None:
            raise last_exc
        if last_response is not None:
            return last_response
        raise RuntimeError(f"{retry_label} request failed without response")

    def get_email(self) -> MailboxAccount:
        self.manager.reload_if_needed()
        account = self.manager.select_account(self.profile_name)
        if not account:
            raise RuntimeError(
                f"cfmail account unavailable, current accounts: {self.manager.account_names()}"
            )

        words = random.sample(_EMAIL_WORDS, 3)
        num = random.randint(0, 999)
        local = random.choice([
            f"{words[0]}_{words[1]}{num}",             # jade_storm42
            f"{words[0]}{num}_{words[1]}",             # jade42_storm
            f"{words[0]}_{words[1]}_{words[2]}",       # jade_storm_echo
            f"{words[0]}_{words[1]}{num}{words[2]}",   # jade_storm42echo
            f"{words[0]}{words[1]}_{num}",             # jadestorm_42
            f"{words[0]}_{num}_{words[1]}",            # jade_42_storm
        ])
        try:
            response = self._request_with_retry(
                method="POST",
                url=f"https://{account.worker_domain}/admin/new_address",
                retry_label="cfmail create mailbox",
                headers={
                    "x-admin-auth": account.admin_password,
                    **cfmail_headers(use_json=True),
                },
                json={
                    "enablePrefix": False,
                    "name": local,
                    "domain": account.email_domain,
                },
                proxies=self.proxies,
                timeout=15,
                impersonate="chrome",
            )
            if response.status_code != 200:
                detail = _response_body_snippet(response)
                detail_suffix = f" | body={detail}" if detail else ""
                raise RuntimeError(f"cfmail create failed: HTTP {response.status_code}{detail_suffix}")
            try:
                data = response.json() if response.content else {}
            except Exception as exc:
                raise RuntimeError(f"cfmail create invalid json: {exc}") from exc
            email = str(data.get("address") or "").strip()
            jwt = str(data.get("jwt") or "").strip()
            if not email or not jwt:
                raise RuntimeError("cfmail create returned incomplete data")
            self.manager.record_success(account.name)
            return MailboxAccount(
                email=email,
                account_id=jwt,
                extra={
                    "api_base": f"https://{account.worker_domain}",
                    "config_name": account.name,
                    "email_domain": account.email_domain,
                },
            )
        except Exception as exc:
            self.manager.record_failure(account.name, f"new_address exception: {exc}")
            raise RuntimeError(str(exc or "cfmail create failed"))

    def get_current_ids(self, account: MailboxAccount) -> set[str]:
        try:
            response = self._request_with_retry(
                method="GET",
                url=f"{account.extra.get('api_base', '')}/api/mails",
                retry_label="cfmail list mails",
                params={"limit": self._mail_list_limit(), "offset": 0},
                headers=cfmail_headers(jwt=account.account_id, use_json=True),
                proxies=self.proxies,
                timeout=15,
                impersonate="chrome",
            )
            if response.status_code != 200:
                return set()
            data = response.json() if response.content else {}
            messages = data.get("results", []) if isinstance(data, dict) else []
            return {
                str(item.get("id") or item.get("createdAt") or "").strip()
                for item in messages
                if isinstance(item, dict) and (item.get("id") or item.get("createdAt"))
            }
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set[str] | None = None,
        not_before_timestamp: float | None = None,
    ) -> str:
        seen_ids = set(before_ids or [])
        api_base = str(account.extra.get("api_base") or "").strip()
        email = account.email.strip().lower()
        config_name = str(account.extra.get("config_name") or "").strip()
        mail_list_limit = self._mail_list_limit()
        patterns = [
            r"Your ChatGPT code is\s*(\d{6})",
            r"ChatGPT\s*(?:代码|验证码)(?:为|是|：|:)\s*(\d{6})",
            r"(?:code is|代码为|验证码)\s*(\d{6})",
            r"temporary verification code to continue:\s*(\d{6})",
            r"(?<!\d)(\d{6})(?!\d)",
        ]
        start = time.time()
        account.extra["otp_wait_started_at"] = start
        diagnostics: dict[str, Any] = {
            "started_at": start,
            "poll_count": 0,
            "message_scan_count": 0,
            "first_message_seen_at": None,
            "matched_message_at": None,
            "matched_message_id": "",
        }
        self.last_wait_diagnostics = diagnostics
        while time.time() - start < timeout:
            try:
                abort_predicate = CFMAIL_WAIT_ABORT_PREDICATE
                if callable(abort_predicate):
                    try:
                        if bool(abort_predicate(account)):
                            diagnostics["aborted"] = True
                            diagnostics["abort_reason"] = "rotation_or_stoploss"
                            self.last_wait_diagnostics = diagnostics
                            if config_name:
                                self.manager.record_failure(config_name, "mail polling aborted")
                            return ""
                    except Exception:
                        pass
                diagnostics["poll_count"] = int(diagnostics.get("poll_count") or 0) + 1
                diagnostics["elapsed_seconds"] = max(0.0, time.time() - start)
                response = self._request_with_retry(
                    method="GET",
                    url=f"{api_base}/api/mails",
                    retry_label="cfmail wait mails",
                    params={"limit": mail_list_limit, "offset": 0},
                    headers=cfmail_headers(jwt=account.account_id, use_json=True),
                    proxies=self.proxies,
                    timeout=15,
                    impersonate="chrome",
                )
                if response.status_code != 200:
                    diagnostics["elapsed_seconds"] = max(0.0, time.time() - start)
                    progress_callback = CFMAIL_WAIT_PROGRESS_CALLBACK
                    if callable(progress_callback):
                        try:
                            progress_callback(account, dict(diagnostics))
                        except Exception:
                            pass
                    time.sleep(3)
                    continue
                data = response.json() if response.content else {}
                messages = data.get("results", []) if isinstance(data, dict) else []
                if not isinstance(messages, list):
                    progress_callback = CFMAIL_WAIT_PROGRESS_CALLBACK
                    if callable(progress_callback):
                        try:
                            progress_callback(account, dict(diagnostics))
                        except Exception:
                            pass
                    time.sleep(3)
                    continue
                for message in messages:
                    if not isinstance(message, dict):
                        continue
                    message_id = str(message.get("id") or message.get("createdAt") or "").strip()
                    if not message_id or message_id in seen_ids:
                        continue
                    message_timestamp = _message_timestamp_seconds(message)
                    if (
                        not_before_timestamp is not None
                        and message_timestamp is not None
                        and message_timestamp < float(not_before_timestamp)
                    ):
                        continue
                    diagnostics["message_scan_count"] = int(diagnostics.get("message_scan_count") or 0) + 1
                    if diagnostics.get("first_message_seen_at") is None:
                        diagnostics["first_message_seen_at"] = time.time()
                    seen_ids.add(message_id)
                    recipient = str(message.get("address") or "").strip().lower()
                    raw = str(message.get("raw") or "")
                    metadata_text = json.dumps(message.get("metadata") or {}, ensure_ascii=False)
                    if recipient and recipient != email:
                        continue
                    # Decode MIME Subject (may span multiple folded lines)
                    decoded_subject = ""
                    subject_match = re.search(
                        r"^Subject:\s*((?:.*(?:\r?\n[ \t]+)?)*.*)$", raw, re.M
                    )
                    if subject_match:
                        raw_subject = re.sub(r"\r?\n[ \t]+", " ", subject_match.group(1)).strip()
                        try:
                            parts = email.header.decode_header(raw_subject)
                            decoded_subject = "".join(
                                p.decode(enc or "utf-8") if isinstance(p, bytes) else str(p)
                                for p, enc in parts
                            )
                        except Exception:
                            decoded_subject = raw_subject
                    # Body starts after the first blank line in the raw email
                    body_start = -1
                    for sep in ("\r\n\r\n", "\n\n"):
                        body_start = raw.find(sep)
                        if body_start >= 0:
                            break
                    mail_body = raw[body_start:] if body_start >= 0 else raw
                    content = "\n".join([recipient, decoded_subject, mail_body, metadata_text])
                    if keyword and keyword.lower() not in content.lower():
                        continue
                    for pattern in patterns:
                        match = re.search(pattern, content, re.I | re.S)
                        if match:
                            diagnostics["matched_message_at"] = time.time()
                            diagnostics["matched_message_id"] = message_id
                            if config_name:
                                self.manager.record_success(config_name)
                            return match.group(1)
                diagnostics["elapsed_seconds"] = max(0.0, time.time() - start)
                progress_callback = CFMAIL_WAIT_PROGRESS_CALLBACK
                if callable(progress_callback):
                    try:
                        progress_callback(account, dict(diagnostics))
                    except Exception:
                        pass
            except Exception:
                diagnostics["elapsed_seconds"] = max(0.0, time.time() - start)
                progress_callback = CFMAIL_WAIT_PROGRESS_CALLBACK
                if callable(progress_callback):
                    try:
                        progress_callback(account, dict(diagnostics))
                    except Exception:
                        pass
            time.sleep(DEFAULT_CFMAIL_WAIT_POLL_INTERVAL_SECONDS)
        if config_name:
            self.manager.record_failure(config_name, "mail polling timeout")
        return ""


DEFAULT_CFMAIL_MANAGER = CfmailAccountManager()

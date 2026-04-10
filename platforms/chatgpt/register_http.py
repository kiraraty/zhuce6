"""HTTP/session helpers for ChatGPT registration."""

from __future__ import annotations

import json
import os
import secrets
import time
import urllib.parse
from typing import Any, Callable

from .constants import (
    DEFAULT_PASSWORD_LENGTH,
    OPENAI_API_ENDPOINTS,
    OPENAI_PAGE_TYPES,
    PASSWORD_CHARSET,
    generate_random_user_info,
)

DEFAULT_ADD_PHONE_OAUTH_OTP_TIMEOUT_SECONDS = 90


def _build_sentinel_header(
    self,
    sentinel: str,
    device_id: str,
    flow: str,
    *,
    client: Any | None = None,
) -> str:
    sentinel_client = client or self.http_client
    build_header = getattr(sentinel_client, "build_sentinel_header", None)
    if callable(build_header):
        try:
            return str(build_header(device_id=device_id, flow=flow, token=sentinel))
        except Exception:
            pass
    return json.dumps(
        {
            "p": "",
            "t": "",
            "c": sentinel,
            "id": device_id,
            "flow": flow,
        },
        separators=(",", ":"),
    )

def _oauth_json_headers(self, *, referer: str, device_id: str) -> dict[str, str]:
    return {
        "accept": "application/json",
        "content-type": "application/json",
        "origin": "https://auth.openai.com",
        "referer": referer,
        "oai-device-id": device_id,
        "user-agent": self.http_client.default_headers.get("User-Agent", "Mozilla/5.0"),
        "sec-ch-ua": self.http_client.default_headers.get("sec-ch-ua", ""),
        "sec-ch-ua-mobile": self.http_client.default_headers.get("sec-ch-ua-mobile", ""),
        "sec-ch-ua-platform": self.http_client.default_headers.get("sec-ch-ua-platform", ""),
    }

def _is_transient_transport_error(self, exc: Exception) -> bool:
    message = str(exc or "").lower()
    markers = (
        "connection closed abruptly",
        "connection timed out",
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

def _session_request(
    self,
    *,
    session: Any,
    method: str,
    url: str,
    label: str,
    refresh_session: Callable[[], Any] | None = None,
    max_attempts: int = 3,
    retry_delay: float = 1.0,
    **kwargs: Any,
) -> tuple[Any, Any]:
    current_session = session
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = getattr(current_session, method.lower())(url, **kwargs)
            return response, current_session
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts or not self._is_transient_transport_error(exc):
                raise
            self._log(f"{label}: transient transport error, retry {attempt}/{max_attempts}: {exc}")
            if refresh_session is not None:
                current_session = refresh_session()
            time.sleep(retry_delay * attempt)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"{label}: request failed without exception")


def _refresh_registration_session(self) -> Any:
    cookie_pairs: dict[str, str] = {}
    current_session = getattr(self, 'session', None)
    try:
        cookies = getattr(current_session, 'cookies', None)
        if cookies is not None:
            jar = getattr(cookies, 'jar', None)
            if jar is not None:
                for item in list(jar):
                    name = str(getattr(item, 'name', '') or '').strip()
                    value = str(getattr(item, 'value', '') or '').strip()
                    if name:
                        cookie_pairs[name] = value
            for key, value in dict(cookies).items():
                if key:
                    cookie_pairs[str(key)] = str(value)
    except Exception:
        pass
    try:
        self.http_client.close()
    except Exception:
        pass
    new_session = self.http_client.session
    try:
        new_session.cookies.update(cookie_pairs)
    except Exception:
        pass
    self.session = new_session
    return new_session


def _check_ip_location(self) -> tuple[bool, str | None]:
    try:
        return self.http_client.check_ip_location()
    except Exception as exc:
        self._log(f"check_ip_location failed: {exc}")
        return False, None

def _create_email(self) -> bool:
    if self.email:
        self.email_info = {"email": self.email}
        self._log(f"using provided mailbox: {self.email}")
        return True
    for attempt in range(1, self.create_email_max_attempts + 1):
        try:
            candidate_info = self.email_service.create_email()
        except Exception as exc:
            self._log(f"create_email failed: {exc}")
            return False
        candidate_email = str((candidate_info or {}).get("email") or "").strip()
        if not candidate_email:
            self._log("create_email returned no email address")
            return False
        if self.mailbox_dedupe_store is not None and not self.mailbox_dedupe_store.reserve(candidate_email):
            self._log(
                f"duplicate mailbox discarded ({attempt}/{self.create_email_max_attempts}): {candidate_email}"
            )
            continue
        self.email_info = candidate_info
        self.email = candidate_email
        self._reserved_email = candidate_email
        self._log(f"created mailbox: {self.email}")
        return True
    self._log("create_email exhausted unique mailbox retries")
    return False

def _init_session(self) -> bool:
    try:
        self.session = self.http_client.session
        return True
    except Exception as exc:
        self._log(f"init_session failed: {exc}")
        return False

def _start_oauth(self) -> bool:
    try:
        self.oauth_start = self.oauth_manager.start_oauth()
        self._log("oauth flow initialized")
        return True
    except Exception as exc:
        self._log(f"oauth init failed: {exc}")
        return False

def _get_device_id(self) -> str | None:
    if not self.oauth_start or self.session is None:
        return None
    try:
        self.session.get(self.oauth_start.auth_url, timeout=15)
        device_id = str(self.session.cookies.get("oai-did") or "").strip()
        if device_id:
            self._log(f"device_id acquired: {device_id}")
            return device_id
        self._log("device_id missing from oauth bootstrap cookies")
        return None
    except Exception as exc:
        self._log(f"get_device_id failed: {exc}")
        return None

def _check_sentinel(self, did: str, *, flow: str = "authorize_continue") -> str | None:
    try:
        token = self.http_client.check_sentinel(did, flow=flow)
        if token:
            self._log("sentinel token acquired")
        else:
            self._log("sentinel token unavailable")
        return token
    except Exception as exc:
        self._log(f"check_sentinel failed: {exc}")
        return None

def _submit_signup_form(self, did: str, sen_token: str | None) -> SignupFormResult:
    if self.session is None or not self.email:
        return SignupFormResult(success=False, error_message="session or email missing")
    try:
        signup_body = json.dumps(
            {
                "username": {"value": self.email, "kind": "email"},
                "screen_hint": "signup",
            }
        )
        headers = {
            "referer": "https://auth.openai.com/create-account",
            "accept": "application/json",
            "content-type": "application/json",
        }
        if sen_token:
            sentinel = self._build_sentinel_header(
                sen_token,
                did,
                "authorize_continue",
            )
            headers["openai-sentinel-token"] = sentinel

        response = self.session.post(
            OPENAI_API_ENDPOINTS["signup"],
            headers=headers,
            data=signup_body,
        )
        self._log(f"signup form status: {response.status_code}")
        if response.status_code != 200:
            return SignupFormResult(
                success=False,
                error_message=f"HTTP {response.status_code}: {response.text[:200]}",
            )

        try:
            response_data = response.json()
        except Exception as exc:
            return SignupFormResult(success=False, error_message=f"signup json parse failed: {exc}")

        page_type = str(((response_data.get("page") or {}).get("type")) or "").strip()
        is_existing = page_type == OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]
        self._is_existing_account = is_existing
        if is_existing:
            self._log("existing account detected; switching to login-like OTP flow")
        else:
            self._log(f"signup page type: {page_type or 'unknown'}")
        return SignupFormResult(
            success=True,
            page_type=page_type,
            is_existing_account=is_existing,
            response_data=response_data,
        )
    except Exception as exc:
        self._log(f"submit_signup_form failed: {exc}")
        return SignupFormResult(success=False, error_message=str(exc))

def _register_password(self, device_id: str = "", sentinel_token: str = "") -> bool:
    if self.session is None or not self.email:
        return False
    try:
        if not self.password:
            self.password = self._generate_password()
        payload = json.dumps({"password": self.password, "username": self.email})
        headers = {
            "referer": "https://auth.openai.com/create-account/password",
            "accept": "application/json",
            "content-type": "application/json",
        }
        if sentinel_token and device_id:
            sentinel = self._build_sentinel_header(
                sentinel_token,
                device_id,
                "authorize_continue",
            )
            headers["openai-sentinel-token"] = sentinel
        response = self.session.post(
            OPENAI_API_ENDPOINTS["register"],
            headers=headers,
            data=payload,
        )
        self._log(f"register password status: {response.status_code}")
        if response.status_code != 200:
            self._log(f"register password failed body: {response.text[:240]}")
            return False
        return True
    except Exception as exc:
        self._log(f"register_password failed: {exc}")
        return False

def _send_verification_code(self) -> bool:
    if self.session is None:
        return False
    try:
        self._signup_otp_before_ids = self._capture_mailbox_ids()
        self._otp_sent_at = time.time()
        self._log(
            "send otp mailbox baseline captured: "
            f"{len(self._signup_otp_before_ids)} existing ids"
        )
        response, session = self._session_request(
            session=self.session,
            method="GET",
            url=OPENAI_API_ENDPOINTS["send_otp"],
            label="send otp",
            refresh_session=self._refresh_registration_session,
            headers={
                "referer": "https://auth.openai.com/create-account/password",
                "accept": "application/json",
            },
        )
        self.session = session
        self._log(f"send otp status: {response.status_code}")
        return response.status_code == 200
    except Exception as exc:
        self._log(f"send_verification_code failed: {exc}")
        return False

def _create_user_account(self) -> bool:
    if self.session is None:
        return False
    try:
        self._last_create_account_http_status = None
        self._last_create_account_error_code = ""
        self._last_create_account_error_message = ""
        self._last_create_account_error_body = ""
        user_info = generate_random_user_info()
        self._log(f"generated profile: {user_info['name']} / {user_info['birthdate']}")
        response = self.session.post(
            OPENAI_API_ENDPOINTS["create_account"],
            headers={
                "referer": "https://auth.openai.com/about-you",
                "accept": "application/json",
                "content-type": "application/json",
            },
            data=json.dumps(user_info),
        )
        self._last_create_account_http_status = int(response.status_code)
        self._log(f"create account status: {response.status_code}")
        if response.status_code != 200:
            self._last_create_account_error_body = str(response.text or "")[:240]
            self._log(f"create account body: {self._last_create_account_error_body}")
            try:
                error_payload = response.json()
            except Exception:
                error_payload = {}
            error_info = error_payload.get("error") if isinstance(error_payload, dict) else {}
            if isinstance(error_info, dict):
                self._last_create_account_error_code = str(error_info.get("code") or "").strip()
                self._last_create_account_error_message = str(error_info.get("message") or "").strip()
            if self._last_create_account_error_code or self._last_create_account_error_message:
                self._log(
                    "create account classified error: "
                    f"code={self._last_create_account_error_code or '-'} "
                    f"message={self._last_create_account_error_message or '-'}"
                )
            return False
        try:
            create_resp = response.json()
            self._log(f"create account response keys: {list(create_resp.keys())}")
            # Store continue_url from response (bypass workspace flow)
            curl = str(create_resp.get("continue_url") or "").strip()
            page_info = create_resp.get("page") or {}
            page_type = str(page_info.get("type") or "").strip() if isinstance(page_info, dict) else ""
            continue_host = ""
            continue_kind = "unknown"
            if curl:
                parsed_curl = urllib.parse.urlparse(curl)
                continue_host = parsed_curl.netloc
                if "callback/openai" in curl:
                    continue_kind = "callback_openai"
                elif "add-phone" in curl:
                    continue_kind = "add_phone"
                elif "workspace" in curl:
                    continue_kind = "workspace"
                else:
                    continue_kind = f"other:{parsed_curl.path[:40]}"
            self._log(
                f"create_account result: page_type={page_type}, "
                f"continue_kind={continue_kind}, continue_host={continue_host}"
            )
            if curl:
                self._create_account_continue_url = curl
                self._log(f"continue_url from create_account: {curl[:120]}")
        except Exception:
            pass
        return True
    except Exception as exc:
        self._log(f"create_user_account failed: {exc}")
        return False


def _email_domain(self) -> str:
    email = str(self.email or "").strip()
    if "@" not in email:
        return ""
    return email.rsplit("@", 1)[-1].strip().lower()

def _load_add_phone_oauth_max_attempts(self) -> int:
    raw = str(os.getenv("ZHUCE6_ADD_PHONE_OAUTH_MAX_ATTEMPTS", "1") or "1").strip()
    try:
        value = int(raw)
    except Exception:
        value = 1
    return max(1, min(value, 3))

def _load_wait_otp_timeout_seconds(self) -> int:
    raw = str(os.getenv("ZHUCE6_WAIT_OTP_TIMEOUT_SECONDS", "180") or "180").strip()
    try:
        value = int(raw)
    except Exception:
        value = 180
    return max(60, min(value, 300))

def _load_add_phone_oauth_otp_timeout_seconds(self) -> int:
    raw = str(
        os.getenv(
            "ZHUCE6_ADD_PHONE_OAUTH_OTP_TIMEOUT_SECONDS",
            str(DEFAULT_ADD_PHONE_OAUTH_OTP_TIMEOUT_SECONDS),
        )
        or str(DEFAULT_ADD_PHONE_OAUTH_OTP_TIMEOUT_SECONDS)
    ).strip()
    try:
        value = int(raw)
    except Exception:
        value = DEFAULT_ADD_PHONE_OAUTH_OTP_TIMEOUT_SECONDS
    return max(30, min(value, 180))

def _load_post_create_login_delay_seconds(self) -> int:
    raw = str(os.getenv("ZHUCE6_POST_CREATE_LOGIN_DELAY_SECONDS", "0") or "0").strip()
    try:
        value = int(raw)
    except Exception:
        value = 0
    return max(0, min(value, 600))

def _metadata(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "email_domain": self._email_domain(),
        "create_account_http_status": self._last_create_account_http_status,
        "create_account_error_code": self._last_create_account_error_code,
        "create_account_error_message": self._last_create_account_error_message,
    }
    if self._last_create_account_error_body:
        payload["create_account_error_body"] = self._last_create_account_error_body
    if self._last_otp_wait_failure_reason:
        payload["otp_wait_failure_reason"] = self._last_otp_wait_failure_reason
    if self._last_otp_wait_diagnostics:
        payload.update(self._last_otp_wait_diagnostics)
    if extra:
        payload.update(extra)
    return payload

def _generate_password(self, length: int = DEFAULT_PASSWORD_LENGTH) -> str:
    return "".join(secrets.choice(PASSWORD_CHARSET) for _ in range(length))

def _auth_url(self, url: str) -> str:
    candidate = str(url or "").strip()
    if not candidate:
        return ""
    return urllib.parse.urljoin("https://auth.openai.com", candidate)

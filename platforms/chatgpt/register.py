"""ChatGPT registration engine for zhuce6."""

from __future__ import annotations

import base64
from dataclasses import asdict, dataclass, field
from datetime import datetime
import logging
import time
from typing import Any, Callable, Protocol

from .constants import OPENAI_PAGE_TYPES
from .http_client import OpenAIHTTPClient
from .oauth import OAuthManager, OAuthStart, submit_callback_url
from . import register_http as register_http_module
from .register_http import (
    _build_sentinel_header,
    _auth_url,
    _check_ip_location,
    _check_sentinel,
    _refresh_registration_session,
    _create_email,
    _create_user_account,
    _email_domain,
    _get_device_id,
    _init_session,
    _is_transient_transport_error,
    _load_add_phone_oauth_max_attempts,
    _load_add_phone_oauth_otp_timeout_seconds,
    _load_post_create_login_delay_seconds,
    _load_wait_otp_timeout_seconds,
    _metadata,
    _oauth_json_headers,
    _generate_password,
    _register_password,
    _send_verification_code,
    _session_request,
    _start_oauth,
    _submit_signup_form,
)
from . import register_oauth as register_oauth_module
from .register_oauth import (
    _decode_oauth_session_cookie,
    _extract_callback_url,
    _extract_callback_url_from_error,
    _extract_session_token,
    _follow_redirects,
    _follow_redirects_with_session,
    _get_workspace_id,
    _handle_oauth_callback,
    _login_for_token as _login_for_token_impl,
    _parse_token_response,
    _parse_workspace_from_cookie,
    _refresh_tokens_from_session_cookie as _refresh_tokens_from_session_cookie_impl,
    _select_workspace,
    _try_create_account_callback_session_token,
    _try_direct_session_token,
    _parse_session_jwt,
)
from .register_otp import (
    _capture_mailbox_ids,
    _get_verification_code,
    _mailbox_context,
    _validate_verification_code,
    _wait_for_mailbox_code,
)
from .token_refresh import TokenRefreshManager

logger = logging.getLogger(__name__)
DEFAULT_ADD_PHONE_OAUTH_OTP_TIMEOUT_SECONDS = 90


class EmailServiceProtocol(Protocol):
    def create_email(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        ...

    def get_verification_code(
        self,
        email: str | None = None,
        email_id: str | None = None,
        timeout: int = 120,
        pattern: str | None = None,
        otp_sent_at: float | None = None,
    ) -> str:
        ...


class MailboxDedupeProtocol(Protocol):
    def reserve(self, email: str) -> bool:
        ...

    def release(self, email: str) -> None:
        ...

    def mark(self, email: str, *, reason: str) -> None:
        ...


@dataclass
class RegistrationResult:
    success: bool
    stage: str = "init"
    email: str = ""
    password: str = ""
    account_id: str = ""
    workspace_id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    session_token: str = ""
    error_message: str = ""
    logs: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    manual_steps: list[str] = field(default_factory=list)
    source: str = "register"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SignupFormResult:
    success: bool
    page_type: str = ""
    is_existing_account: bool = False
    response_data: dict[str, Any] = field(default_factory=dict)
    error_message: str = ""


register_http_module.SignupFormResult = SignupFormResult


class RegistrationEngine:
    """Repaired ChatGPT registration flow with truthful runtime stages."""

    def __init__(
        self,
        email_service: EmailServiceProtocol,
        proxy_url: str | None = None,
        callback_logger: Callable[[str], None] | None = None,
        task_uuid: str | None = None,
        mailbox_dedupe_store: MailboxDedupeProtocol | None = None,
        create_email_max_attempts: int = 5,
    ) -> None:
        self.email_service = email_service
        self.proxy_url = proxy_url
        self.callback_logger = callback_logger or (lambda message: logger.info(message))
        self.task_uuid = task_uuid
        self.mailbox_dedupe_store = mailbox_dedupe_store
        self.create_email_max_attempts = max(1, int(create_email_max_attempts))
        self.http_client = OpenAIHTTPClient(proxy_url=proxy_url)
        self.oauth_manager = OAuthManager(proxy_url=proxy_url)
        self.email: str | None = None
        self.password: str | None = None
        self.email_info: dict[str, Any] | None = None
        self.oauth_start: OAuthStart | None = None
        self.session: Any | None = None
        self.session_token: str | None = None
        self.logs: list[str] = []
        self._otp_sent_at: float | None = None
        self._signup_otp_before_ids: set[str] = set()
        self._is_existing_account = False
        self._create_account_continue_url: str | None = None
        self._last_create_account_http_status: int | None = None
        self._last_create_account_error_code: str = ""
        self._last_create_account_error_message: str = ""
        self._last_create_account_error_body: str = ""
        self._add_phone_oauth_max_attempts = self._load_add_phone_oauth_max_attempts()
        self._otp_wait_timeout_seconds = self._load_wait_otp_timeout_seconds()
        self._add_phone_oauth_otp_timeout_seconds = self._load_add_phone_oauth_otp_timeout_seconds()
        self._post_create_login_delay_seconds = self._load_post_create_login_delay_seconds()
        self._last_otp_wait_failure_reason: str = ""
        self._last_otp_wait_diagnostics: dict[str, Any] = {}
        self._reserved_email: str = ""

    def _log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_message = f"[{timestamp}] {message}"
        self.logs.append(log_message)
        self.callback_logger(log_message)

    def _result(
        self,
        *,
        success: bool,
        stage: str,
        error_message: str = "",
        source: str = "register",
        metadata: dict[str, Any] | None = None,
        manual_steps: list[str] | None = None,
    ) -> RegistrationResult:
        merged_metadata = self._metadata(metadata)
        return RegistrationResult(
            success=success,
            stage=stage,
            email=self.email or "",
            password=self.password or "",
            account_id="",
            workspace_id="",
            error_message=error_message,
            logs=list(self.logs),
            metadata=merged_metadata,
            manual_steps=manual_steps or [],
            source=source,
        )

    def _sync_oauth_helper_globals(self) -> None:
        register_oauth_module.OpenAIHTTPClient = OpenAIHTTPClient
        register_oauth_module.OPENAI_PAGE_TYPES = OPENAI_PAGE_TYPES
        register_oauth_module.TokenRefreshManager = TokenRefreshManager
        register_oauth_module.submit_callback_url = submit_callback_url
        register_oauth_module.base64 = base64
        register_oauth_module.re = __import__("re")

    def _refresh_tokens_from_session_cookie(
        self,
        session: Any | None = None,
        *,
        label: str,
    ) -> dict[str, Any] | None:
        self._sync_oauth_helper_globals()
        return _refresh_tokens_from_session_cookie_impl(self, session, label=label)

    def _login_for_token(self) -> dict[str, Any] | None:
        self._sync_oauth_helper_globals()
        return _login_for_token_impl(self)

    def run_preflight(self) -> RegistrationResult:
        if not self.password:
            self.password = self._generate_password()
        ip_ok, location = self._check_ip_location()
        if not ip_ok:
            return self._result(
                success=False,
                stage="ip_check",
                error_message=f"unsupported or unknown ip location: {location}",
                source="register_preflight",
            )
        if not self._create_email():
            return self._result(
                success=False,
                stage="mailbox",
                error_message="mailbox bootstrap failed",
                source="register_preflight",
            )
        if not self._init_session():
            return self._result(
                success=False,
                stage="session",
                error_message="session bootstrap failed",
                source="register_preflight",
            )
        if not self._start_oauth():
            return self._result(
                success=False,
                stage="oauth_bootstrap",
                error_message="oauth bootstrap failed",
                source="register_preflight",
            )
        device_id = self._get_device_id()
        sentinel_token = self._check_sentinel(device_id) if device_id else None
        self._log("registration preflight ready")
        return RegistrationResult(
            success=False,
            stage="oauth_preflight",
            email=self.email or "",
            password=self.password or "",
            error_message="full registration flow requires live upstream interaction; preflight is ready",
            logs=list(self.logs),
            metadata=self._metadata(
                {
                    "location": location,
                    "device_id": device_id or "",
                    "sentinel_token_present": bool(sentinel_token),
                    "oauth_url": self.oauth_start.auth_url if self.oauth_start else "",
                    "oauth_state": self.oauth_start.state if self.oauth_start else "",
                    "oauth_code_verifier": self.oauth_start.code_verifier if self.oauth_start else "",
                    "oauth_redirect_uri": self.oauth_start.redirect_uri if self.oauth_start else "",
                    "task_uuid": self.task_uuid or "",
                }
            ),
            manual_steps=[
                "Open oauth_url in a browser if you want to continue manually.",
                "Use callback exchange if you capture a real callback URL.",
            ],
            source="register_preflight",
        )

    def run(self) -> RegistrationResult:
        result = RegistrationResult(success=False, stage="init", logs=list(self.logs))

        try:
            self._log("=" * 60)
            self._log("starting chatgpt registration flow")
            self._log("=" * 60)

            ip_ok, location = self._check_ip_location()
            if not ip_ok:
                return self._result(
                    success=False,
                    stage="ip_check",
                    error_message=f"unsupported or unknown ip location: {location}",
                    metadata={"location": location},
                )

            if not self._create_email():
                return self._result(success=False, stage="mailbox", error_message="create email failed")

            if not self._init_session():
                return self._result(success=False, stage="session", error_message="session bootstrap failed")

            if not self._start_oauth():
                return self._result(success=False, stage="oauth_bootstrap", error_message="oauth bootstrap failed")

            device_id = self._get_device_id()
            if not device_id:
                return self._result(success=False, stage="device_id", error_message="device id acquisition failed")

            sentinel_token = self._check_sentinel(device_id)
            signup_result = self._submit_signup_form(device_id, sentinel_token)
            if not signup_result.success:
                return self._result(
                    success=False,
                    stage="signup",
                    error_message=signup_result.error_message or "signup form failed",
                    metadata={"page_type": signup_result.page_type},
                )

            if self._is_existing_account:
                self._otp_sent_at = time.time()
                self._log("existing account flow: skipping password registration and otp send")
            else:
                if not self._register_password():
                    return self._result(success=False, stage="password", error_message="password registration failed")
                time.sleep(1)
                if self.session is not None:
                    try:
                        self.session.get(
                            "https://auth.openai.com/create-account/password",
                            headers={"referer": "https://auth.openai.com/create-account"},
                        )
                    except Exception:
                        pass
                if not self._send_verification_code():
                    return self._result(success=False, stage="send_otp", error_message="otp send failed")

            code = self._get_verification_code()
            if not code:
                return self._result(success=False, stage="wait_otp", error_message="otp retrieval failed")

            if not self._validate_verification_code(code):
                return self._result(success=False, stage="validate_otp", error_message="otp validation failed")

            if not self._is_existing_account and not self._create_user_account():
                if (
                    self.mailbox_dedupe_store is not None
                    and self.email
                    and self._last_create_account_error_code.strip().lower() == "user_already_exists"
                ):
                    self.mailbox_dedupe_store.mark(self.email, reason="user_already_exists")
                return self._result(success=False, stage="create_account", error_message="create account failed")

            post_create_continue_url = self._auth_url(str(self._create_account_continue_url or "").strip())
            post_create_gate = ""
            if not self._is_existing_account and "add-phone" in post_create_continue_url:
                post_create_gate = "add_phone"
                self._log(
                    "post-create continue_url requires phone gate; "
                    "continuing oauth token acquisition attempt"
                )

            token_info: dict[str, Any] | None = None
            workspace_id = ""
            continue_url = ""
            callback_url = ""
            if not token_info:
                token_info = self._try_create_account_callback_session_token(post_create_continue_url)
            if not token_info:
                workspace_id = self._get_workspace_id()
                if workspace_id:
                    continue_url = self._select_workspace(workspace_id) or ""
                    if continue_url:
                        callback_url = self._follow_redirects(continue_url) or ""
                        if callback_url:
                            token_info = self._handle_oauth_callback(callback_url)

            if not token_info:
                if post_create_gate == "add_phone":
                    self._log("post-create add_phone: attempting direct session token extraction")
                    token_info = self._try_direct_session_token()

            if not token_info:
                max_oauth_attempts = 1
                if post_create_gate == "add_phone":
                    max_oauth_attempts = self._add_phone_oauth_max_attempts
                for oauth_attempt in range(1, max_oauth_attempts + 1):
                    if oauth_attempt == 1:
                        if post_create_gate == "add_phone" and self._post_create_login_delay_seconds > 0:
                            self._log(
                                "post-create add_phone: waiting before fresh login "
                                f"({self._post_create_login_delay_seconds}s)"
                            )
                            time.sleep(self._post_create_login_delay_seconds)
                        self._log("workspace flow failed; attempting password login for token")
                    else:
                        self._log(
                            "add-phone oauth retry: "
                            f"attempt {oauth_attempt}/{max_oauth_attempts}"
                        )
                    token_info = self._login_for_token()
                    if token_info:
                        break

            if not token_info:
                if post_create_gate == "add_phone":
                    # Solution B: include credentials for deferred retry queue
                    mailbox_account = getattr(self.email_service, "_account", None)
                    deferred_info: dict[str, Any] = {
                        "email": self.email or "",
                        "password": self.password or "",
                    }
                    if mailbox_account is not None:
                        deferred_info["mailbox_jwt"] = str(getattr(mailbox_account, "account_id", "") or "")
                        deferred_info["mailbox_extra"] = dict(getattr(mailbox_account, "extra", {}) or {})
                    return self._result(
                        success=False,
                        stage="add_phone_gate",
                        error_message="post-create flow requires phone gate",
                        metadata={
                            "post_create_continue_url": post_create_continue_url,
                            "post_create_gate": post_create_gate,
                            "deferred_credentials": deferred_info,
                        },
                    )
                return self._result(
                    success=False,
                    stage="token_acquisition",
                    error_message="all token acquisition methods exhausted",
                )

            session_cookie = ""
            if self.session is not None:
                session_cookie = str(self.session.cookies.get("__Secure-next-auth.session-token") or "").strip()
            if not session_cookie:
                session_cookie = str((token_info or {}).get("session_token") or "").strip()

            result = RegistrationResult(
                success=True,
                stage="completed",
                email=self.email or "",
                password=self.password or "",
                account_id=str((token_info or {}).get("account_id") or "").strip(),
                workspace_id=workspace_id or "",
                access_token=str((token_info or {}).get("access_token") or "").strip(),
                refresh_token=str((token_info or {}).get("refresh_token") or "").strip(),
                id_token=str((token_info or {}).get("id_token") or "").strip(),
                session_token=session_cookie,
                logs=list(self.logs),
                metadata={
                    "location": location,
                    "device_id": device_id,
                    "page_type": signup_result.page_type,
                    "is_existing_account": self._is_existing_account,
                    "continue_url": continue_url or "",
                    "callback_url": callback_url or "",
                    "has_oauth_token": bool(token_info),
                    "expired": str((token_info or {}).get("expired") or ""),
                    "last_refresh": str((token_info or {}).get("last_refresh") or ""),
                    "email_domain": self._email_domain(),
                    "create_account_http_status": self._last_create_account_http_status,
                    "create_account_error_code": self._last_create_account_error_code,
                    "create_account_error_message": self._last_create_account_error_message,
                    "post_create_gate": post_create_gate,
                    "post_create_continue_url": post_create_continue_url,
                },
                source="login" if self._is_existing_account else "register",
            )
            self._log("=" * 60)
            self._log(f"registration flow finished successfully for {result.email}")
            self._log("=" * 60)
            return result

        except Exception as exc:
            self._log(f"unexpected registration error: {exc}")
            return self._result(success=False, stage="unexpected_error", error_message=str(exc))
        finally:
            if self.mailbox_dedupe_store is not None and self._reserved_email:
                self.mailbox_dedupe_store.release(self._reserved_email)


for _name, _func in {
    '_build_sentinel_header': _build_sentinel_header,
    '_auth_url': _auth_url,
    '_oauth_json_headers': _oauth_json_headers,
    '_extract_callback_url': _extract_callback_url,
    '_extract_callback_url_from_error': _extract_callback_url_from_error,
    '_extract_session_token': _extract_session_token,
    '_is_transient_transport_error': _is_transient_transport_error,
    '_session_request': _session_request,
    '_decode_oauth_session_cookie': _decode_oauth_session_cookie,
    '_mailbox_context': _mailbox_context,
    '_capture_mailbox_ids': _capture_mailbox_ids,
    '_wait_for_mailbox_code': _wait_for_mailbox_code,
    '_check_ip_location': _check_ip_location,
    '_email_domain': _email_domain,
    '_create_email': _create_email,
    '_generate_password': _generate_password,
    '_init_session': _init_session,
    '_start_oauth': _start_oauth,
    '_get_device_id': _get_device_id,
    '_check_sentinel': _check_sentinel,
    '_refresh_registration_session': _refresh_registration_session,
    '_submit_signup_form': _submit_signup_form,
    '_register_password': _register_password,
    '_send_verification_code': _send_verification_code,
    '_get_verification_code': _get_verification_code,
    '_validate_verification_code': _validate_verification_code,
    '_create_user_account': _create_user_account,
    '_extract_callback_url': _extract_callback_url,
    '_extract_callback_url_from_error': _extract_callback_url_from_error,
    '_extract_session_token': _extract_session_token,
    '_follow_redirects_with_session': _follow_redirects_with_session,
    '_parse_token_response': _parse_token_response,
    '_parse_workspace_from_cookie': _parse_workspace_from_cookie,
    '_load_add_phone_oauth_max_attempts': _load_add_phone_oauth_max_attempts,
    '_load_wait_otp_timeout_seconds': _load_wait_otp_timeout_seconds,
    '_load_add_phone_oauth_otp_timeout_seconds': _load_add_phone_oauth_otp_timeout_seconds,
    '_load_post_create_login_delay_seconds': _load_post_create_login_delay_seconds,
    '_metadata': _metadata,
    '_get_workspace_id': _get_workspace_id,
    '_select_workspace': _select_workspace,
    '_follow_redirects': _follow_redirects,
    '_handle_oauth_callback': _handle_oauth_callback,
    '_try_create_account_callback_session_token': _try_create_account_callback_session_token,
    '_try_direct_session_token': _try_direct_session_token,
    '_parse_session_jwt': _parse_session_jwt,
}.items():
    setattr(RegistrationEngine, _name, _func)

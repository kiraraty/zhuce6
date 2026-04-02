"""Token refresh helpers for the zhuce6 ChatGPT platform."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any

from curl_cffi import requests as cffi_requests

from .constants import (
    OAUTH_CLIENT_ID,
    OAUTH_REDIRECT_URI,
    OPENAI_IMPERSONATE,
    OPENAI_SEC_CH_UA,
    OPENAI_SEC_CH_UA_MOBILE,
    OPENAI_SEC_CH_UA_PLATFORM,
    OPENAI_USER_AGENT,
)

logger = logging.getLogger(__name__)


@dataclass
class TokenRefreshResult:
    success: bool
    access_token: str = ""
    refresh_token: str = ""
    account_id: str = ""
    email: str = ""
    session_token: str = ""
    expires_at: datetime | None = None
    error_message: str = ""


class TokenRefreshManager:
    SESSION_URL = "https://chatgpt.com/api/auth/session"
    TOKEN_URL = "https://auth.openai.com/oauth/token"

    def __init__(self, proxy_url: str | None = None) -> None:
        self.proxy_url = proxy_url
        self._oauth_client_id = OAUTH_CLIENT_ID
        self._oauth_redirect_uri = OAUTH_REDIRECT_URI

    @property
    def _default_headers(self) -> dict[str, str]:
        return {
            "user-agent": OPENAI_USER_AGENT,
            "accept-language": "en-US,en;q=0.9",
            "sec-ch-ua": OPENAI_SEC_CH_UA,
            "sec-ch-ua-mobile": OPENAI_SEC_CH_UA_MOBILE,
            "sec-ch-ua-platform": OPENAI_SEC_CH_UA_PLATFORM,
        }

    def _create_session(self) -> cffi_requests.Session:
        return cffi_requests.Session(impersonate=OPENAI_IMPERSONATE, proxy=self.proxy_url)

    def refresh_by_session_token(self, session_token: str) -> TokenRefreshResult:
        result = TokenRefreshResult(success=False)
        try:
            session = self._create_session()
            session.cookies.set(
                "__Secure-next-auth.session-token",
                session_token,
                domain=".chatgpt.com",
                path="/",
            )
            response = session.get(
                self.SESSION_URL,
                headers={**self._default_headers, "accept": "application/json"},
                timeout=30,
            )
            if response.status_code != 200:
                result.error_message = f"Session token refresh failed: HTTP {response.status_code}"
                return result
            data = response.json()
            access_token = str(data.get("accessToken") or "").strip()
            if not access_token:
                result.error_message = "Session token refresh failed: missing accessToken"
                return result
            expires_at = None
            expires_str = str(data.get("expires") or "").strip()
            if expires_str:
                try:
                    expires_at = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
                except ValueError:
                    expires_at = None
            result.success = True
            result.access_token = access_token
            user = data.get("user") or {}
            result.account_id = str(data.get("account_id") or (user.get("id") if isinstance(user, dict) else "") or "").strip()
            result.email = str((user.get("email") if isinstance(user, dict) else "") or data.get("email") or "").strip()
            result.session_token = session_token
            result.expires_at = expires_at
            return result
        except Exception as exc:
            result.error_message = f"Session token refresh exception: {exc}"
            logger.error(result.error_message)
            return result

    def refresh_by_oauth_token(self, refresh_token: str, client_id: str | None = None) -> TokenRefreshResult:
        result = TokenRefreshResult(success=False)
        try:
            session = self._create_session()
            response = session.post(
                self.TOKEN_URL,
                headers={
                    **self._default_headers,
                    "content-type": "application/x-www-form-urlencoded",
                    "accept": "application/json",
                },
                data={
                    "client_id": client_id or self._oauth_client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "redirect_uri": self._oauth_redirect_uri,
                },
                timeout=30,
            )
            if response.status_code != 200:
                result.error_message = f"OAuth token refresh failed: HTTP {response.status_code}"
                return result
            data = response.json()
            access_token = str(data.get("access_token") or "").strip()
            if not access_token:
                result.error_message = "OAuth token refresh failed: missing access_token"
                return result
            result.success = True
            result.access_token = access_token
            result.refresh_token = str(data.get("refresh_token") or refresh_token).strip()
            result.expires_at = datetime.utcnow() + timedelta(seconds=int(data.get("expires_in", 3600)))
            return result
        except Exception as exc:
            result.error_message = f"OAuth token refresh exception: {exc}"
            logger.error(result.error_message)
            return result

    def refresh_account(self, account: Any) -> TokenRefreshResult:
        session_token = str(getattr(account, "session_token", "") or "").strip()
        if session_token:
            session_result = self.refresh_by_session_token(session_token)
            if session_result.success:
                return session_result
        refresh_token = str(getattr(account, "refresh_token", "") or "").strip()
        if refresh_token:
            return self.refresh_by_oauth_token(
                refresh_token=refresh_token,
                client_id=str(getattr(account, "client_id", "") or "").strip() or None,
            )
        return TokenRefreshResult(success=False, error_message="No session_token or refresh_token available")

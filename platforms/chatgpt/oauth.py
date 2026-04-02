"""OAuth helpers for the zhuce6 ChatGPT platform."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

from curl_cffi import requests as cffi_requests

from .constants import (
    OAUTH_AUTH_URL,
    OAUTH_CLIENT_ID,
    OPENAI_IMPERSONATE,
    OPENAI_SEC_CH_UA,
    OPENAI_SEC_CH_UA_MOBILE,
    OPENAI_SEC_CH_UA_PLATFORM,
    OPENAI_USER_AGENT,
    OAUTH_REDIRECT_URI,
    OAUTH_SCOPE,
    OAUTH_TOKEN_URL,
)


def _b64url_no_pad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _sha256_b64url_no_pad(value: str) -> str:
    return _b64url_no_pad(hashlib.sha256(value.encode("ascii")).digest())


def _random_state(nbytes: int = 16) -> str:
    return secrets.token_urlsafe(nbytes)


def _pkce_verifier() -> str:
    return secrets.token_urlsafe(64)


def _parse_callback_url(callback_url: str) -> dict[str, str]:
    candidate = callback_url.strip()
    if not candidate:
        return {"code": "", "state": "", "error": "", "error_description": ""}
    if "://" not in candidate:
        if candidate.startswith("?"):
            candidate = f"http://localhost{candidate}"
        elif "=" in candidate:
            candidate = f"http://localhost/?{candidate}"
        else:
            candidate = f"http://{candidate}"

    parsed = urllib.parse.urlparse(candidate)
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    fragment = urllib.parse.parse_qs(parsed.fragment, keep_blank_values=True)
    for key, values in fragment.items():
        if key not in query or not query[key]:
            query[key] = values

    def get1(key: str) -> str:
        return str((query.get(key, [""])[0] or "")).strip()

    return {
        "code": get1("code"),
        "state": get1("state"),
        "error": get1("error"),
        "error_description": get1("error_description"),
    }


def _jwt_claims_no_verify(id_token: str) -> dict[str, Any]:
    if not id_token or id_token.count(".") < 2:
        return {}
    payload_b64 = id_token.split(".")[1]
    pad = "=" * ((4 - (len(payload_b64) % 4)) % 4)
    try:
        payload = base64.urlsafe_b64decode((payload_b64 + pad).encode("ascii"))
        return json.loads(payload.decode("utf-8"))
    except Exception:
        return {}


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _post_form(
    url: str,
    data: dict[str, str],
    timeout: int = 30,
    proxy_url: str | None = None,
) -> dict[str, Any]:
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    response = cffi_requests.post(
        url,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": OPENAI_USER_AGENT,
            "sec-ch-ua": OPENAI_SEC_CH_UA,
            "sec-ch-ua-mobile": OPENAI_SEC_CH_UA_MOBILE,
            "sec-ch-ua-platform": OPENAI_SEC_CH_UA_PLATFORM,
        },
        timeout=timeout,
        proxies=proxies,
        impersonate=OPENAI_IMPERSONATE,
    )
    if response.status_code != 200:
        raise RuntimeError(f"token exchange failed: {response.status_code}: {response.text}")
    return response.json()


@dataclass(frozen=True)
class OAuthStart:
    auth_url: str
    state: str
    code_verifier: str
    redirect_uri: str


def generate_oauth_url(
    *,
    redirect_uri: str = OAUTH_REDIRECT_URI,
    scope: str = OAUTH_SCOPE,
    client_id: str = OAUTH_CLIENT_ID,
) -> OAuthStart:
    state = _random_state()
    code_verifier = _pkce_verifier()
    code_challenge = _sha256_b64url_no_pad(code_verifier)
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": "login",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
    }
    auth_url = f"{OAUTH_AUTH_URL}?{urllib.parse.urlencode(params)}"
    return OAuthStart(
        auth_url=auth_url,
        state=state,
        code_verifier=code_verifier,
        redirect_uri=redirect_uri,
    )


def submit_callback_url(
    *,
    callback_url: str,
    expected_state: str,
    code_verifier: str,
    redirect_uri: str = OAUTH_REDIRECT_URI,
    client_id: str = OAUTH_CLIENT_ID,
    token_url: str = OAUTH_TOKEN_URL,
    proxy_url: str | None = None,
) -> str:
    callback = _parse_callback_url(callback_url)
    if callback["error"]:
        raise RuntimeError(f"oauth error: {callback['error']}: {callback['error_description']}".strip())
    if not callback["code"]:
        raise ValueError("callback url missing ?code=")
    if not callback["state"]:
        raise ValueError("callback url missing ?state=")
    if callback["state"] != expected_state:
        raise ValueError("state mismatch")

    token_resp = _post_form(
        token_url,
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": callback["code"],
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
        proxy_url=proxy_url,
    )

    access_token = str(token_resp.get("access_token") or "").strip()
    refresh_token = str(token_resp.get("refresh_token") or "").strip()
    id_token = str(token_resp.get("id_token") or "").strip()
    expires_in = _to_int(token_resp.get("expires_in"))

    claims = _jwt_claims_no_verify(id_token)
    email = str(claims.get("email") or "").strip()
    auth_claims = claims.get("https://api.openai.com/auth") or {}
    account_id = str(auth_claims.get("chatgpt_account_id") or "").strip()

    now = int(time.time())
    expired_rfc3339 = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + max(expires_in, 0)))
    now_rfc3339 = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    config = {
        "id_token": id_token,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "account_id": account_id,
        "last_refresh": now_rfc3339,
        "email": email,
        "type": "codex",
        "expired": expired_rfc3339,
    }
    return json.dumps(config, ensure_ascii=False, separators=(",", ":"))


class OAuthManager:
    def __init__(
        self,
        client_id: str = OAUTH_CLIENT_ID,
        auth_url: str = OAUTH_AUTH_URL,
        token_url: str = OAUTH_TOKEN_URL,
        redirect_uri: str = OAUTH_REDIRECT_URI,
        scope: str = OAUTH_SCOPE,
        proxy_url: str | None = None,
    ) -> None:
        self.client_id = client_id
        self.auth_url = auth_url
        self.token_url = token_url
        self.redirect_uri = redirect_uri
        self.scope = scope
        self.proxy_url = proxy_url

    def start_oauth(self) -> OAuthStart:
        return generate_oauth_url(
            redirect_uri=self.redirect_uri,
            scope=self.scope,
            client_id=self.client_id,
        )

    def handle_callback(self, callback_url: str, expected_state: str, code_verifier: str) -> dict[str, Any]:
        return json.loads(
            submit_callback_url(
                callback_url=callback_url,
                expected_state=expected_state,
                code_verifier=code_verifier,
                redirect_uri=self.redirect_uri,
                client_id=self.client_id,
                token_url=self.token_url,
                proxy_url=self.proxy_url,
            )
        )

"""CPA upload helpers for the zhuce6 ChatGPT platform."""

from __future__ import annotations

import json
from typing import Any

from curl_cffi import CurlMime
from curl_cffi import requests as cffi_requests

from .constants import (
    OPENAI_IMPERSONATE,
    OPENAI_SEC_CH_UA,
    OPENAI_SEC_CH_UA_MOBILE,
    OPENAI_SEC_CH_UA_PLATFORM,
    OPENAI_USER_AGENT,
)


def _upload_url(api_url: str) -> str:
    return f"{api_url.rstrip('/')}/v0/management/auth-files"


def _headers(api_key: str | None) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key or ''}",
        "User-Agent": OPENAI_USER_AGENT,
        "sec-ch-ua": OPENAI_SEC_CH_UA,
        "sec-ch-ua-mobile": OPENAI_SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": OPENAI_SEC_CH_UA_PLATFORM,
    }


def _error_message(response: Any) -> str:
    base = f"upload failed: HTTP {response.status_code}"
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        message = str(payload.get("message") or payload.get("error") or "").strip()
        if message:
            return message
    text = str(getattr(response, "text", "") or "").strip()
    if text:
        return f"{base} - {text[:200]}"
    return base


def generate_token_json(account: Any) -> dict[str, str]:
    expires_at = getattr(account, "expires_at", None)
    last_refresh = getattr(account, "last_refresh", None)
    return {
        "type": "codex",
        "email": str(getattr(account, "email", "") or "").strip(),
        "expired": expires_at.strftime("%Y-%m-%dT%H:%M:%S+08:00") if expires_at else "",
        "id_token": str(getattr(account, "id_token", "") or "").strip(),
        "account_id": str(getattr(account, "account_id", "") or "").strip(),
        "access_token": str(getattr(account, "access_token", "") or "").strip(),
        "last_refresh": last_refresh.strftime("%Y-%m-%dT%H:%M:%S+08:00") if last_refresh else "",
        "refresh_token": str(getattr(account, "refresh_token", "") or "").strip(),
    }


def upload_to_cpa(
    token_data: dict[str, str],
    api_url: str | None = None,
    api_key: str | None = None,
    proxy: str | None = None,
) -> tuple[bool, str]:
    del proxy  # CPA is direct-connect by default in zhuce6.
    if not api_url:
        return False, "CPA API URL is required"

    upload_url = _upload_url(api_url)
    payload = json.dumps(token_data, ensure_ascii=False, indent=2).encode("utf-8")

    mime = CurlMime()
    mime.addpart(
        name="file",
        data=payload,
        filename=f"{token_data.get('email', 'account')}.json",
        content_type="application/json",
    )
    try:
        response = cffi_requests.post(
            upload_url,
            multipart=mime,
            headers=_headers(api_key),
            timeout=30,
            impersonate=OPENAI_IMPERSONATE,
        )
    except Exception as exc:
        return False, f"upload exception: {exc}"
    if response.status_code in {200, 201}:
        return True, "upload success"
    return False, _error_message(response)


def upload_to_team_manager(account: Any, api_url: str | None = None, api_key: str | None = None) -> tuple[bool, str]:
    ok, message = upload_to_cpa(
        generate_token_json(account),
        api_url=api_url,
        api_key=api_key,
        proxy=None,
    )
    if ok:
        return True, "team manager upload success"
    return False, message


def test_cpa_connection(api_url: str | None = None, api_key: str | None = None) -> tuple[bool, str]:
    if not api_url:
        return False, "CPA API URL is required"

    try:
        response = cffi_requests.options(
            _upload_url(api_url),
            headers=_headers(api_key),
            timeout=10,
            impersonate=OPENAI_IMPERSONATE,
        )
    except Exception as exc:
        return False, f"connection failed: {exc}"

    if response.status_code in {200, 204, 401, 403, 405}:
        if response.status_code == 401:
            return False, "connection reached server but API key is invalid"
        return True, "connection ok"
    return False, f"connection failed: HTTP {response.status_code}"

"""OpenAI-specific HTTP client helpers for zhuce6."""

from __future__ import annotations

import json
import logging
from typing import Any

from curl_cffi import requests as cffi_requests

from core.http_client import HTTPClient, HTTPClientError, RequestConfig
from .constants import (
    OPENAI_API_ENDPOINTS,
    OPENAI_IMPERSONATE,
    OPENAI_SEC_CH_UA,
    OPENAI_SEC_CH_UA_MOBILE,
    OPENAI_SEC_CH_UA_PLATFORM,
    OPENAI_USER_AGENT,
)
from .sentinel_pow import SentinelTokenGenerator

logger = logging.getLogger(__name__)


class OpenAIHTTPClient(HTTPClient):
    def __init__(self, proxy_url: str | None = None, config: RequestConfig | None = None) -> None:
        resolved_config = config or RequestConfig(impersonate=OPENAI_IMPERSONATE)
        super().__init__(proxy_url=proxy_url, config=resolved_config)
        self._sentinel_payloads: dict[tuple[str, str], dict[str, str]] = {}
        self.default_headers = {
            "User-Agent": OPENAI_USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "sec-ch-ua": OPENAI_SEC_CH_UA,
            "sec-ch-ua-mobile": OPENAI_SEC_CH_UA_MOBILE,
            "sec-ch-ua-platform": OPENAI_SEC_CH_UA_PLATFORM,
        }

    def check_ip_location(self) -> tuple[bool, str | None]:
        try:
            response = self.get("https://cloudflare.com/cdn-cgi/trace", timeout=10)
            for line in response.text.splitlines():
                if line.startswith("loc="):
                    loc = line.split("=", 1)[1].strip()
                    if loc == "CN":
                        return False, loc
                    return True, loc
        except Exception as exc:
            logger.warning("IP location check failed, proceeding anyway: %s", exc)
        return True, None

    def send_openai_request(
        self,
        endpoint: str,
        method: str = "POST",
        data: Any = None,
        json_data: Any = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        request_headers = self.default_headers.copy()
        if headers:
            request_headers.update(headers)
        try:
            response = self.request(
                method,
                endpoint,
                data=data,
                json=json_data,
                headers=request_headers,
                **kwargs,
            )
            response.raise_for_status()
            try:
                return response.json()
            except json.JSONDecodeError:
                return {"raw_response": response.text}
        except cffi_requests.RequestsError as exc:
            raise HTTPClientError(f"OpenAI request failed: {endpoint} - {exc}") from exc

    def build_sentinel_header(self, *, device_id: str, flow: str, token: str = "") -> str:
        payload = self._sentinel_payloads.get((str(device_id or "").strip(), str(flow or "").strip()))
        if payload:
            return json.dumps(payload, separators=(",", ":"))
        return json.dumps(
            {
                "p": "",
                "t": "",
                "c": str(token or "").strip(),
                "id": str(device_id or "").strip(),
                "flow": str(flow or "").strip(),
            },
            separators=(",", ":"),
        )

    def check_sentinel(self, did: str, *, flow: str = "authorize_continue") -> str | None:
        try:
            device_id = str(did or "").strip()
            resolved_flow = str(flow or "authorize_continue").strip() or "authorize_continue"
            generator = SentinelTokenGenerator(
                device_id=device_id,
                user_agent=self.default_headers.get("User-Agent"),
            )
            sen_req_body = json.dumps(
                {
                    "p": generator.generate_requirements_token(),
                    "id": device_id,
                    "flow": resolved_flow,
                },
                separators=(",", ":"),
            )
            response = self.post(
                OPENAI_API_ENDPOINTS["sentinel"],
                headers={
                    "origin": "https://sentinel.openai.com",
                    "referer": (
                        "https://sentinel.openai.com/backend-api/"
                        "sentinel/frame.html?sv=20260219f9f6"
                    ),
                    "content-type": "text/plain;charset=UTF-8",
                    "sec-ch-ua": OPENAI_SEC_CH_UA,
                    "sec-ch-ua-mobile": OPENAI_SEC_CH_UA_MOBILE,
                    "sec-ch-ua-platform": OPENAI_SEC_CH_UA_PLATFORM,
                },
                data=sen_req_body,
            )
            if response.status_code == 200:
                payload = response.json()
                token = str(payload.get("token") or "").strip()
                if not token:
                    return None
                pow_data = payload.get("proofofwork") or {}
                if isinstance(pow_data, dict) and pow_data.get("required") and pow_data.get("seed"):
                    p_value = generator.generate_token(
                        seed=str(pow_data.get("seed") or ""),
                        difficulty=str(pow_data.get("difficulty") or "0"),
                    )
                else:
                    p_value = generator.generate_requirements_token()
                self._sentinel_payloads[(device_id, resolved_flow)] = {
                    "p": p_value,
                    "t": "",
                    "c": token,
                    "id": device_id,
                    "flow": resolved_flow,
                }
                return token
        except Exception as exc:
            logger.warning("Sentinel request failed: %s", exc)
        return None

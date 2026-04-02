"""Sub2API admin HTTP client."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class Sub2ApiClient:
    def __init__(self, base_url, admin_email, admin_password, api_key="", timeout=20):
        self.base_url = str(base_url or "http://127.0.0.1:8080").strip().rstrip("/") or "http://127.0.0.1:8080"
        self.admin_email = str(admin_email or "").strip()
        self.admin_password = str(admin_password or "").strip()
        self.api_key = str(api_key or "").strip()
        self.timeout = max(1, int(timeout))
        self._jwt = ""

    def _ensure_jwt(self) -> str:
        if self.api_key:
            return ""
        if self._jwt:
            return self._jwt
        body = json.dumps({"email": self.admin_email, "password": self.admin_password}, ensure_ascii=False).encode("utf-8")
        payload = self._request_raw("POST", "/api/v1/auth/login", body=body, with_auth=False)
        data = payload.get("data", payload) if isinstance(payload, dict) else {}
        token = str((data or {}).get("access_token") or "").strip()
        if not token:
            raise RuntimeError("sub2api login returned empty access_token")
        self._jwt = token
        return token

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        else:
            headers["Authorization"] = f"Bearer {self._ensure_jwt()}"
        return headers

    def _request_raw(self, method: str, path: str, body: bytes | None = None, *, with_auth: bool = True) -> dict[str, Any]:
        url = f"{self.base_url}/{str(path or '').lstrip('/')}"
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if with_auth:
            headers.update(self._headers())
        request = Request(url, data=body, headers=headers, method=str(method or "GET").upper())
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                payload = {"message": raw or str(exc)}
            payload.setdefault("code", exc.code)
            raise RuntimeError(json.dumps(payload, ensure_ascii=False)) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"sub2api request failed: {exc}") from exc
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"sub2api returned invalid json: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("sub2api returned non-object payload")
        return payload

    def _request(self, method, path, body=None) -> dict:
        encoded_body = None
        if body is not None:
            encoded_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        for attempt in range(2):
            try:
                payload = self._request_raw(method, path, body=encoded_body)
                data = payload.get("data", payload)
                return data if isinstance(data, dict) else {"items": data} if isinstance(data, list) else {}
            except RuntimeError as exc:
                message = str(exc)
                if '"code": 401' in message and not self.api_key and attempt == 0:
                    self._jwt = ""
                    self._ensure_jwt()
                    continue
                raise
        return {}

    def health_check(self) -> bool:
        request = Request(f"{self.base_url}/health", headers={"Accept": "application/json"}, method="GET")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8") or "{}")
        except Exception:
            return False
        return isinstance(payload, dict) and payload.get("status") == "ok"

    def list_accounts(self, platform="openai", page=1, page_size=100) -> dict:
        query = urlencode({"platform": platform, "page": page, "page_size": page_size})
        return self._request("GET", f"/api/v1/admin/accounts?{query}")

    def create_account(self, name, credentials, platform="openai", type="oauth", **kwargs) -> dict:
        payload = {
            "name": name,
            "platform": platform,
            "type": type,
            "credentials": credentials,
        }
        payload.update({key: value for key, value in kwargs.items() if value is not None})
        return self._request("POST", "/api/v1/admin/accounts", payload)

    def batch_create_accounts(self, accounts: list[dict]) -> dict:
        return self._request("POST", "/api/v1/admin/accounts/batch", {"accounts": accounts})

    def get_account(self, account_id: int) -> dict | None:
        try:
            return self._request("GET", f"/api/v1/admin/accounts/{int(account_id)}")
        except RuntimeError as exc:
            if '"code": 404' in str(exc):
                return None
            raise

    def delete_account(self, account_id: int) -> bool:
        self._request("DELETE", f"/api/v1/admin/accounts/{int(account_id)}")
        return True

    def update_account(self, account_id: int, updates: dict) -> dict:
        return self._request("PUT", f"/api/v1/admin/accounts/{int(account_id)}", updates)

    def refresh_account(self, account_id: int) -> dict:
        return self._request("POST", f"/api/v1/admin/accounts/{int(account_id)}/refresh", {})

    def batch_refresh(self, account_ids: list[int] | None = None) -> dict:
        payload = {}
        if account_ids is not None:
            payload["account_ids"] = account_ids
        return self._request("POST", "/api/v1/admin/accounts/batch-refresh", payload)

    def test_account(self, account_id: int) -> dict:
        return self._request("POST", f"/api/v1/admin/accounts/{int(account_id)}/test", {})

    def set_schedulable(self, account_id: int, schedulable: bool) -> dict:
        return self._request(
            "POST",
            f"/api/v1/admin/accounts/{int(account_id)}/schedulable",
            {"schedulable": bool(schedulable)},
        )

    def clear_error(self, account_id: int) -> dict:
        return self._request("POST", f"/api/v1/admin/accounts/{int(account_id)}/clear-error", {})

    def restart(self) -> dict:
        return self._request("POST", "/api/v1/admin/system/restart", {})

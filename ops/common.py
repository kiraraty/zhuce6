"""Shared helpers for zhuce6 operations."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
import subprocess
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import uuid

PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_POOL_DIR = PROJECT_DIR / "pool"
DEFAULT_MANAGEMENT_BASE_URL = "http://127.0.0.1:8317/v0/management"


def now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def run_command(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def get_management_key() -> str | None:
    return str(os.getenv("ZHUCE6_CPA_MANAGEMENT_KEY", "")).strip() or None


def _normalize_management_base_url(base_url: str) -> str:
    return str(base_url or DEFAULT_MANAGEMENT_BASE_URL).strip().rstrip("/") or DEFAULT_MANAGEMENT_BASE_URL


def cpa_management_request(
    method: str,
    path: str,
    key: str,
    *,
    management_base_url: str = DEFAULT_MANAGEMENT_BASE_URL,
    body: bytes | None = None,
    content_type: str | None = None,
    timeout: int = 20,
    query: dict[str, object] | None = None,
    accept: str = "application/json",
) -> tuple[int, dict | list | str | None]:
    """Send a request to the CPA management API.

    Returns (http_status_code, parsed_payload_or_text_or_None).
    On connection/timeout errors returns (0, None).
    """
    base_url = _normalize_management_base_url(management_base_url)
    url = f"{base_url}/{path.lstrip('/')}"
    if query:
        encoded_query = urlencode({k: v for k, v in query.items() if v is not None}, doseq=True)
        if encoded_query:
            url = f"{url}?{encoded_query}"
    headers = {"Authorization": f"Bearer {key}", "Accept": accept}
    if content_type:
        headers["Content-Type"] = content_type

    request = Request(url, data=body, headers=headers, method=method.upper())
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            try:
                payload: dict | list | str | None = json.loads(raw)
            except json.JSONDecodeError:
                payload = raw
            return response.status, payload
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = raw or None
        return exc.code, payload
    except (URLError, TimeoutError, OSError):
        return 0, None


class CpaClient:
    """CPA management HTTP API client."""

    def __init__(
        self,
        base_url: str,
        *,
        management_key: str | None = None,
        timeout: int = 20,
    ) -> None:
        self.base_url = _normalize_management_base_url(base_url)
        self.management_key = str(management_key or "").strip() or None
        self.timeout = max(1, int(timeout))

    @classmethod
    def from_settings(cls, settings: object, *, timeout: int = 20) -> "CpaClient":
        return cls(
            getattr(settings, "cpa_management_base_url", DEFAULT_MANAGEMENT_BASE_URL),
            management_key=getattr(settings, "cpa_management_key", None),
            timeout=timeout,
        )

    def _resolve_key(self) -> str | None:
        if self.management_key:
            return self.management_key
        self.management_key = get_management_key()
        return self.management_key

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        query: dict[str, object] | None = None,
        accept: str = "application/json",
    ) -> tuple[int, dict | list | str | None]:
        key = self._resolve_key()
        if not key:
            return 0, None
        return cpa_management_request(
            method,
            path,
            key,
            management_base_url=self.base_url,
            body=body,
            content_type=content_type,
            timeout=self.timeout,
            query=query,
            accept=accept,
        )

    def list_auth_files(self) -> list[dict[str, object]]:
        status, payload = self._request("GET", "auth-files")
        if status == 0:
            return []
        if isinstance(payload, dict):
            files = payload.get("files", payload.get("auth_files", []))
        elif isinstance(payload, list):
            files = payload
        else:
            files = []
        return [item for item in files if isinstance(item, dict)]

    def get_auth_file(self, name: str) -> dict[str, object] | None:
        normalized_name = str(name or "").strip()
        if not normalized_name:
            return None
        status, payload = self._request(
            "GET",
            "auth-files/download",
            query={"name": normalized_name},
            accept="application/json, text/plain;q=0.9, */*;q=0.8",
        )
        if status == 0 or payload is None:
            return None
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, str):
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                return None
            return parsed if isinstance(parsed, dict) else None
        return None

    def delete_auth_file(self, name: str) -> bool:
        return self.delete_auth_files([name])

    def delete_auth_files(self, names: list[str]) -> bool:
        normalized_names = [str(name or "").strip() for name in names if str(name or "").strip()]
        if not normalized_names:
            return True
        for normalized_name in normalized_names:
            status, payload = self._request(
                "DELETE",
                "auth-files",
                query={"name": normalized_name},
            )
            if status not in {200, 204}:
                preview = str(payload)
                if len(preview) > 200:
                    preview = preview[:200] + "..."
                print(f"[{now()}] [警告] CPA delete 失败 | status={status} | files=1 | {preview}")
                return False
        return True

    def delete_all_auth_files(self) -> bool:
        status, _payload = self._request("DELETE", "auth-files", query={"all": "true"})
        return status in {200, 204}

    def upload_auth_file(self, name: str, content: dict[str, object]) -> bool:
        normalized_name = str(name or "").strip()
        if not normalized_name:
            return False
        boundary = f"----zhuce6-{uuid.uuid4().hex}"
        file_bytes = json.dumps(content, ensure_ascii=False, indent=2).encode("utf-8")
        multipart = b"".join(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="file"; filename="{normalized_name}"\r\n'.encode("utf-8"),
                b"Content-Type: application/json\r\n\r\n",
                file_bytes,
                b"\r\n",
                f"--{boundary}--\r\n".encode("utf-8"),
            ]
        )
        status, payload = self._request(
            "POST",
            "auth-files",
            body=multipart,
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        if status not in {200, 201, 204}:
            preview = str(payload)
            if len(preview) > 200:
                preview = preview[:200] + "..."
            print(f"[{now()}] [警告] CPA upload 失败 | status={status} | name={normalized_name} | {preview}")
        return status in {200, 201, 204}

    def restart_container(self) -> bool:
        status, _payload = self._request("POST", "restart")
        return status in {200, 202, 204}

    def api_call(
        self,
        *,
        auth_index: str,
        method: str,
        url: str,
        headers: dict[str, object] | None = None,
        body: str | None = None,
        timeout: int | None = None,
    ) -> dict[str, object]:
        payload = {
            "authIndex": str(auth_index or "").strip(),
            "method": str(method or "GET").strip().upper() or "GET",
            "url": str(url or "").strip(),
            "header": headers or {},
        }
        if body is not None:
            payload["body"] = body
        key = self._resolve_key()
        if not key:
            return {}
        status, response_payload = cpa_management_request(
            "POST",
            "api-call",
            key,
            management_base_url=self.base_url,
            body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            content_type="application/json",
            timeout=timeout or max(self.timeout, 60),
        )
        if status == 0 or not isinstance(response_payload, dict):
            return {}
        return response_payload

    def health_check(self) -> bool:
        status, _payload = self._request("GET", "auth-files")
        return status in {200, 401, 403}


def create_backend_client(settings):
    """根据 settings.backend 创建对应 client."""
    backend = str(getattr(settings, "backend", "cpa") or "cpa").strip().lower() or "cpa"
    if backend == "sub2api":
        from ops.sub2api_adapter import Sub2ApiAdapter
        from ops.sub2api_client import Sub2ApiClient

        client = Sub2ApiClient(
            base_url=getattr(settings, "sub2api_base_url", "http://127.0.0.1:8080"),
            admin_email=getattr(settings, "sub2api_admin_email", ""),
            admin_password=getattr(settings, "sub2api_admin_password", ""),
            api_key=getattr(settings, "sub2api_api_key", ""),
            timeout=20,
        )
        return Sub2ApiAdapter(client)
    return CpaClient.from_settings(settings, timeout=20)

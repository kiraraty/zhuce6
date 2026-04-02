import json
from urllib.error import HTTPError

import pytest

from ops.sub2api_client import Sub2ApiClient


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self.payload = payload
        self.status = status

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeHttpError(HTTPError):
    def __init__(self, url: str, code: int, payload: dict):
        super().__init__(url, code, "error", hdrs=None, fp=None)
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def _decode_body(request) -> dict:
    data = request.data
    assert data is not None
    return json.loads(data.decode("utf-8"))


def test_login_caches_jwt(monkeypatch):
    calls: list[tuple[str, str, dict | None]] = []

    def fake_urlopen(request, timeout=0):
        body = _decode_body(request) if request.data else None
        calls.append((request.get_method(), request.full_url, body))
        if request.full_url.endswith("/api/v1/auth/login"):
            return FakeResponse({"code": 0, "data": {"access_token": "jwt-1"}})
        return FakeResponse({"code": 0, "data": {"items": [], "total": 0, "page": 1, "page_size": 100, "pages": 1}})

    monkeypatch.setattr("ops.sub2api_client.urlopen", fake_urlopen)
    client = Sub2ApiClient("http://example.test", "admin@example.com", "secret")

    first = client._ensure_jwt()
    second = client._ensure_jwt()

    assert first == "jwt-1"
    assert second == "jwt-1"
    assert calls == [
        (
            "POST",
            "http://example.test/api/v1/auth/login",
            {"email": "admin@example.com", "password": "secret"},
        )
    ]


def test_create_account_sends_correct_payload(monkeypatch):
    seen: dict[str, object] = {}

    def fake_urlopen(request, timeout=0):
        if request.full_url.endswith("/api/v1/auth/login"):
            return FakeResponse({"code": 0, "data": {"access_token": "jwt-1"}})
        seen["headers"] = dict(request.header_items())
        seen["body"] = _decode_body(request)
        return FakeResponse({"code": 0, "data": {"id": 9, "name": "demo"}})

    monkeypatch.setattr("ops.sub2api_client.urlopen", fake_urlopen)
    client = Sub2ApiClient("http://example.test", "admin@example.com", "secret")

    payload = client.create_account(
        name="demo",
        credentials={"refresh_token": "rt", "access_token": "at", "email": "demo@example.com"},
        platform="openai",
        type="oauth",
        concurrency=2,
        priority=5,
    )

    assert payload == {"id": 9, "name": "demo"}
    assert seen["body"] == {
        "name": "demo",
        "platform": "openai",
        "type": "oauth",
        "credentials": {"refresh_token": "rt", "access_token": "at", "email": "demo@example.com"},
        "concurrency": 2,
        "priority": 5,
    }
    assert seen["headers"]["Authorization"] == "Bearer jwt-1"


def test_batch_create(monkeypatch):
    def fake_urlopen(request, timeout=0):
        if request.full_url.endswith("/api/v1/auth/login"):
            return FakeResponse({"code": 0, "data": {"access_token": "jwt-1"}})
        assert request.full_url.endswith("/api/v1/admin/accounts/batch")
        assert _decode_body(request) == {"accounts": [{"name": "a"}, {"name": "b"}]}
        return FakeResponse({"code": 0, "data": {"success": 2, "failed": 0, "results": []}})

    monkeypatch.setattr("ops.sub2api_client.urlopen", fake_urlopen)
    client = Sub2ApiClient("http://example.test", "admin@example.com", "secret")

    assert client.batch_create_accounts([{"name": "a"}, {"name": "b"}]) == {"success": 2, "failed": 0, "results": []}


def test_list_accounts_with_pagination(monkeypatch):
    def fake_urlopen(request, timeout=0):
        if request.full_url.endswith("/api/v1/auth/login"):
            return FakeResponse({"code": 0, "data": {"access_token": "jwt-1"}})
        assert request.full_url == (
            "http://example.test/api/v1/admin/accounts?platform=openai&page=2&page_size=50"
        )
        return FakeResponse(
            {"code": 0, "data": {"items": [{"id": 1}], "total": 1, "page": 2, "page_size": 50, "pages": 1}}
        )

    monkeypatch.setattr("ops.sub2api_client.urlopen", fake_urlopen)
    client = Sub2ApiClient("http://example.test", "admin@example.com", "secret")

    payload = client.list_accounts(page=2, page_size=50)

    assert payload["items"] == [{"id": 1}]
    assert payload["page"] == 2


def test_delete_account(monkeypatch):
    def fake_urlopen(request, timeout=0):
        if request.full_url.endswith("/api/v1/auth/login"):
            return FakeResponse({"code": 0, "data": {"access_token": "jwt-1"}})
        assert request.get_method() == "DELETE"
        assert request.full_url.endswith("/api/v1/admin/accounts/7")
        return FakeResponse({"code": 0, "data": {"message": "Account deleted successfully"}})

    monkeypatch.setattr("ops.sub2api_client.urlopen", fake_urlopen)
    client = Sub2ApiClient("http://example.test", "admin@example.com", "secret")

    assert client.delete_account(7) is True


def test_refresh_account(monkeypatch):
    def fake_urlopen(request, timeout=0):
        if request.full_url.endswith("/api/v1/auth/login"):
            return FakeResponse({"code": 0, "data": {"access_token": "jwt-1"}})
        assert request.full_url.endswith("/api/v1/admin/accounts/3/refresh")
        return FakeResponse({"code": 0, "data": {"message": "refresh queued"}})

    monkeypatch.setattr("ops.sub2api_client.urlopen", fake_urlopen)
    client = Sub2ApiClient("http://example.test", "admin@example.com", "secret")

    assert client.refresh_account(3) == {"message": "refresh queued"}


def test_health_check(monkeypatch):
    def fake_urlopen(request, timeout=0):
        assert request.full_url == "http://example.test/health"
        return FakeResponse({"status": "ok"})

    monkeypatch.setattr("ops.sub2api_client.urlopen", fake_urlopen)
    client = Sub2ApiClient("http://example.test", "admin@example.com", "secret")

    assert client.health_check() is True


def test_401_retry_refreshes_jwt(monkeypatch):
    calls: list[str] = []

    def fake_urlopen(request, timeout=0):
        calls.append(request.full_url)
        if request.full_url.endswith("/api/v1/auth/login"):
            token = "jwt-1" if calls.count(request.full_url) == 1 else "jwt-2"
            return FakeResponse({"code": 0, "data": {"access_token": token}})
        auth_header = dict(request.header_items()).get("Authorization")
        if auth_header == "Bearer jwt-1":
            raise FakeHttpError(request.full_url, 401, {"code": 401, "message": "unauthorized"})
        return FakeResponse({"code": 0, "data": {"id": 42}})

    monkeypatch.setattr("ops.sub2api_client.urlopen", fake_urlopen)
    client = Sub2ApiClient("http://example.test", "admin@example.com", "secret")

    assert client.get_account(42) == {"id": 42}
    assert calls == [
        "http://example.test/api/v1/auth/login",
        "http://example.test/api/v1/admin/accounts/42",
        "http://example.test/api/v1/auth/login",
        "http://example.test/api/v1/admin/accounts/42",
    ]


def test_api_key_mode_skips_login(monkeypatch):
    seen_headers: list[dict[str, str]] = []

    def fake_urlopen(request, timeout=0):
        seen_headers.append(dict(request.header_items()))
        return FakeResponse({"code": 0, "data": {"items": [], "total": 0, "page": 1, "page_size": 100, "pages": 1}})

    monkeypatch.setattr("ops.sub2api_client.urlopen", fake_urlopen)
    client = Sub2ApiClient("http://example.test", "", "", api_key="key-123")

    client.list_accounts()

    assert len(seen_headers) == 1
    headers = {key.lower(): value for key, value in seen_headers[0].items()}
    assert headers["x-api-key"] == "key-123"
    assert "authorization" not in headers

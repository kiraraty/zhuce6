from __future__ import annotations

from types import SimpleNamespace

import pytest

from ops.common import CpaClient, create_backend_client


def test_cpa_client_delete_auth_file_uses_name_query(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_request(method: str, path: str, key: str, **kwargs):  # type: ignore[no-untyped-def]
        calls.append({
            "method": method,
            "path": path,
            "key": key,
            "query": kwargs.get("query"),
            "body": kwargs.get("body"),
            "content_type": kwargs.get("content_type"),
        })
        return 200, {"status": "ok"}

    monkeypatch.setattr("ops.common.cpa_management_request", fake_request)
    client = CpaClient(
        "http://127.0.0.1:8317/v0/management",
        management_key="secret",
    )

    assert client.delete_auth_file("alpha@example.com.json") is True
    assert calls == [
        {
            "method": "DELETE",
            "path": "auth-files",
            "key": "secret",
            "query": {"name": "alpha@example.com.json"},
            "body": None,
            "content_type": None,
        }
    ]


def test_cpa_client_delete_auth_files_deletes_each_name_separately(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_request(method: str, path: str, key: str, **kwargs):  # type: ignore[no-untyped-def]
        calls.append({
            "method": method,
            "path": path,
            "key": key,
            "query": kwargs.get("query"),
            "body": kwargs.get("body"),
            "content_type": kwargs.get("content_type"),
        })
        return 200, {"status": "ok"}

    monkeypatch.setattr("ops.common.cpa_management_request", fake_request)
    client = CpaClient(
        "http://127.0.0.1:8317/v0/management",
        management_key="secret",
    )

    assert client.delete_auth_files(["alpha@example.com.json", "beta@example.com.json"]) is True
    assert calls == [
        {
            "method": "DELETE",
            "path": "auth-files",
            "key": "secret",
            "query": {"name": "alpha@example.com.json"},
            "body": None,
            "content_type": None,
        },
        {
            "method": "DELETE",
            "path": "auth-files",
            "key": "secret",
            "query": {"name": "beta@example.com.json"},
            "body": None,
            "content_type": None,
        },
    ]


def test_cpa_client_upload_auth_file_posts_multipart(monkeypatch) -> None:
    captured: list[dict[str, object]] = []

    def fake_request(method: str, path: str, key: str, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(
            {
                "method": method,
                "path": path,
                "key": key,
                "content_type": kwargs.get("content_type"),
                "body": kwargs.get("body"),
            }
        )
        return 201, {"status": "ok"}

    monkeypatch.setattr("ops.common.cpa_management_request", fake_request)
    client = CpaClient("http://127.0.0.1:8317/v0/management", management_key="secret")

    ok = client.upload_auth_file("alpha@example.com.json", {"email": "alpha@example.com", "refresh_token": "rt"})

    assert ok is True
    assert captured[0]["method"] == "POST"
    assert captured[0]["path"] == "auth-files"
    assert "multipart/form-data" in str(captured[0]["content_type"])
    assert b"alpha@example.com.json" in captured[0]["body"]  # type: ignore[operator]


def test_cpa_client_health_check_uses_http_only(monkeypatch) -> None:
    monkeypatch.setattr("ops.common.cpa_management_request", lambda *args, **kwargs: (200, {"files": []}))
    client = CpaClient("http://127.0.0.1:8317/v0/management", management_key="secret")

    assert client.health_check() is True


def test_cpa_client_rejects_removed_legacy_kwargs() -> None:
    with pytest.raises(TypeError):
        CpaClient("http://127.0.0.1:8317/v0/management", management_key="secret", management_mode="http")


def test_create_backend_client_returns_cpa_client() -> None:
    settings = SimpleNamespace(
        backend="cpa",
        cpa_management_base_url="http://127.0.0.1:8317/v0/management",
        cpa_management_key="secret",
    )

    client = create_backend_client(settings)

    assert isinstance(client, CpaClient)
    assert client.base_url == "http://127.0.0.1:8317/v0/management"
    assert client.management_key == "secret"

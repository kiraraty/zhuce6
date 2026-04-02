"""Tests for ops.rotate API-only single-pool logic."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops.rotate import RotateResult, rotate_once
from ops.rotate_probe import classify_status_message


class FakeClient:
    def __init__(self, files: list[dict[str, object]], *, healthy: bool = True):
        self.files = [dict(item) for item in files]
        self.healthy = healthy
        self.deleted: list[str] = []

    def health_check(self) -> bool:
        return self.healthy

    def list_auth_files(self) -> list[dict[str, object]]:
        return [dict(item) for item in self.files]

    def delete_auth_file(self, name: str) -> bool:
        self.deleted.append(name)
        self.files = [item for item in self.files if str(item.get("name")) != name]
        return True


@pytest.fixture(autouse=True)
def _stub_runtime_reconcile(monkeypatch):
    monkeypatch.setattr("ops.rotate._maybe_reconcile_cpa_runtime", lambda **kwargs: None)


def test_classify_unauthorized_returns_401() -> None:
    assert classify_status_message("unauthorized") == 401
    assert classify_status_message("Token invalidated by provider") == 401


def test_classify_quota_returns_429() -> None:
    msg = json.dumps({"error": {"type": "usage_limit_reached", "message": "weekly quota exceeded"}})
    assert classify_status_message(msg) == 429


def test_classify_empty_returns_200() -> None:
    assert classify_status_message("") == 200
    assert classify_status_message(None) == 200  # type: ignore[arg-type]


def test_rotate_deletes_401(tmp_path: Path) -> None:
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    target = pool_dir / "bad@example.com.json"
    target.write_text('{"email": "bad@example.com"}', encoding="utf-8")
    client = FakeClient([{"name": "bad@example.com.json", "status_message": "unauthorized", "status": "error"}])

    result = rotate_once(pool_dir=pool_dir, client=client)

    assert result.deleted_401 == 1
    assert result.deleted_429 == 0
    assert result.main_pool_before == 1
    assert result.main_pool_after == 0
    assert client.deleted == ["bad@example.com.json"]
    assert not target.exists()


def test_rotate_keeps_429(tmp_path: Path) -> None:
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    target = pool_dir / "quota@example.com.json"
    target.write_text('{"email": "quota@example.com"}', encoding="utf-8")
    client = FakeClient(
        [
            {
                "name": "quota@example.com.json",
                "status_message": json.dumps({"error": {"type": "usage_limit_reached", "message": "quota"}}),
                "status": "error",
            }
        ]
    )

    result = rotate_once(pool_dir=pool_dir, client=client)

    assert result.deleted_401 == 0
    assert result.deleted_429 == 0
    assert client.deleted == []
    assert target.exists()


def test_rotate_deletes_deactivated(tmp_path: Path) -> None:
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    target = pool_dir / "dead@example.com.json"
    target.write_text('{"email": "dead@example.com"}', encoding="utf-8")
    client = FakeClient(
        [
            {
                "name": "dead@example.com.json",
                "status_message": json.dumps({"error": {"type": "account_deactivated", "message": "has been deactivated"}}),
                "status": "error",
            }
        ]
    )

    result = rotate_once(pool_dir=pool_dir, client=client)

    assert result.deleted_401 == 1
    assert client.deleted == ["dead@example.com.json"]
    assert not target.exists()


def test_rotate_keeps_transport_error(tmp_path: Path) -> None:
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    target = pool_dir / "retry@example.com.json"
    target.write_text('{"email": "retry@example.com"}', encoding="utf-8")
    client = FakeClient(
        [{"name": "retry@example.com.json", "status_message": 'Post "https://chatgpt.com/backend-api/codex/responses": EOF', "status": "error"}]
    )

    result = rotate_once(pool_dir=pool_dir, client=client)

    assert result.deleted_401 == 0
    assert result.deleted_429 == 0
    assert target.exists()


def test_rotate_keeps_healthy(tmp_path: Path) -> None:
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    target = pool_dir / "ok@example.com.json"
    target.write_text('{"email": "ok@example.com"}', encoding="utf-8")
    client = FakeClient([{"name": "ok@example.com.json", "status_message": "", "status": "active"}])

    result = rotate_once(pool_dir=pool_dir, client=client)

    assert result.deleted_401 == 0
    assert result.deleted_429 == 0
    assert result.quota_probed == 0
    assert target.exists()


def test_rotate_quota_probe_detects_401(monkeypatch, tmp_path: Path) -> None:
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    target = pool_dir / "probe401@example.com.json"
    target.write_text('{"email": "probe401@example.com"}', encoding="utf-8")

    class FakeCpaClient(FakeClient):
        def _resolve_key(self):  # noqa: ANN202
            return "test-key"

    client = FakeCpaClient(
        [
            {
                "name": "probe401@example.com.json",
                "status_message": 'Post "https://chatgpt.com/backend-api/codex/responses": EOF',
                "status": "error",
                "provider": "codex",
                "auth_index": "auth-1",
                "id_token": {"chatgpt_account_id": "acct-1"},
            }
        ]
    )
    monkeypatch.setattr(
        "ops.rotate._collect_quota_probe_results",
        lambda entries, **kwargs: (
            {"probe401@example.com.json": (401, "invalidated", False)},
            {"probed": 1, "probe_401": 1, "probe_429": 0, "probe_skipped": 0},
        ),
    )

    result = rotate_once(pool_dir=pool_dir, client=client)

    assert result.quota_probed == 0  # non-CpaClient client path skips internal key resolution
    assert result.deleted_401 == 0
    assert target.exists()


def test_rotate_cpa_unreachable_returns_empty(tmp_path: Path) -> None:
    client = FakeClient([], healthy=False)

    result = rotate_once(pool_dir=tmp_path / "pool", client=client)

    assert result == RotateResult()


def test_rotate_result_fields_correct(tmp_path: Path) -> None:
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    for name in ["bad@example.com.json", "quota@example.com.json", "ok@example.com.json"]:
        (pool_dir / name).write_text("{}", encoding="utf-8")
    client = FakeClient(
        [
            {"name": "bad@example.com.json", "status_message": "unauthorized", "status": "error"},
            {"name": "quota@example.com.json", "status_message": json.dumps({"error": {"type": "usage_limit_reached", "message": "quota"}}), "status": "error"},
            {"name": "ok@example.com.json", "status_message": "", "status": "active"},
        ]
    )

    result = rotate_once(pool_dir=pool_dir, client=client)

    assert result.main_pool_before == 3
    assert result.deleted_401 == 1
    assert result.deleted_429 == 0
    assert result.main_pool_after == 2

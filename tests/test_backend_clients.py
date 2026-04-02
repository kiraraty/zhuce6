from __future__ import annotations

from pathlib import Path

from core.settings import AppSettings
from dashboard.api import _build_background_tasks
from ops.cleanup import cleanup_once
from ops.rotate import rotate_once
from ops.update_priority import update_priority_once
from ops.validate import validate_once


class FakeBackendClient:
    def __init__(self) -> None:
        self.deleted: list[list[str]] = []
        self.uploads: list[tuple[str, dict]] = []
        self.files = {
            "expired@example.com.json": {
                "email": "expired@example.com",
                "refresh_token": "",
                "expired": "2000-01-01T00:00:00+00:00",
            },
            "priority@example.com.json": {
                "email": "priority@example.com",
                "refresh_token": "rt",
                "priority": 100,
            },
            "invalid@example.com.json": {
                "email": "invalid@example.com",
                "refresh_token": "rt",
                "account_id": "acct-1",
            },
        }

    def health_check(self) -> bool:
        return True

    def list_auth_files(self) -> list[dict[str, object]]:
        return [{"name": name} for name in self.files]

    def get_auth_file(self, name: str) -> dict | None:
        payload = self.files.get(name)
        return dict(payload) if isinstance(payload, dict) else None

    def delete_auth_file(self, name: str) -> bool:
        self.deleted.append([name])
        self.files.pop(name, None)
        return True

    def upload_auth_file(self, name: str, payload: dict) -> bool:
        self.uploads.append((name, dict(payload)))
        self.files[name] = dict(payload)
        return True


def test_cleanup_once_accepts_backend_client(tmp_path: Path) -> None:
    client = FakeBackendClient()

    checked, deleted, refreshed = cleanup_once(client=client, proxy=None, pool_dir=tmp_path)

    assert checked == 3
    assert deleted == 1
    assert refreshed == 0
    assert client.deleted == [["expired@example.com.json"]]


def test_validate_once_accepts_backend_client(monkeypatch, tmp_path: Path) -> None:
    client = FakeBackendClient()
    for name in client.files:
        (tmp_path / name).write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        "ops.validate._validate_file",
        lambda path, _auth_meta: type(
            "FakeEntry",
            (),
            {
                "name": path.name,
                "status_code": 401,
                "action": "delete",
                "detail": "invalid",
                "auth_index": "",
                "account_id": "acct-1",
                "to_dict": lambda self: {
                    "name": path.name,
                    "status_code": 401,
                    "action": "delete",
                    "detail": "invalid",
                    "auth_index": "",
                    "account_id": "acct-1",
                },
            },
        )(),
    )

    summary = validate_once(client=client, proxy=None, dry_run=False, max_workers=1, scope="all", pool_dir=tmp_path)

    assert summary["checked"] == 3
    assert summary["deleted"] == 3
    assert client.deleted == [
        ["expired@example.com.json"],
        ["invalid@example.com.json"],
        ["priority@example.com.json"],
    ]
    assert list(tmp_path.glob("*.json")) == []


def test_update_priority_once_accepts_backend_client() -> None:
    client = FakeBackendClient()

    summary = update_priority_once(client=client, target_priority=500, dry_run=False, limit=1)

    assert summary["total"] == 1
    assert summary["modified"] == 1
    assert client.uploads[0][0] == "expired@example.com.json"
    assert client.uploads[0][1]["priority"] == 500


def test_rotate_once_accepts_backend_client(tmp_path: Path) -> None:
    client = FakeBackendClient()

    result = rotate_once(pool_dir=tmp_path, client=client)

    assert result.main_pool_before == 3
    assert result.main_pool_after == 3
    assert result.deleted_401 == 0


def test_build_background_tasks_uses_backend_client_factory(monkeypatch, tmp_path: Path) -> None:
    created: list[str] = []
    fake_client = object()
    seen: list[tuple[str, object]] = []

    monkeypatch.setattr("dashboard.api.create_backend_client", lambda settings: created.append(settings.backend) or fake_client)
    monkeypatch.setattr("dashboard.api._cleanup_once", lambda **kwargs: seen.append(("cleanup", kwargs["client"])))
    monkeypatch.setattr(
        "dashboard.api._validate_once",
        lambda **kwargs: {"checked": 0, "deleted": 0} if not seen.append(("validate", kwargs["client"])) else None,
    )
    monkeypatch.setattr("dashboard.api._print_validate_summary", lambda summary: summary)
    monkeypatch.setattr(
        "dashboard.api._rotate_once",
        lambda **kwargs: {"main_pool_before": 0} if not seen.append(("rotate", kwargs["client"])) else None,
    )
    monkeypatch.setattr("dashboard.api._print_rotate_summary", lambda summary: summary)

    settings = AppSettings(
        runtime_mode="full",
        backend="sub2api",
        cleanup_enabled=True,
        validate_enabled=True,
        rotate_enabled=True,
        d1_cleanup_enabled=False,
        account_survival_enabled=False,
        pool_dir=tmp_path,
    )

    tasks = _build_background_tasks(settings)
    for task in tasks:
        task.fn()

    assert created == ["sub2api", "sub2api", "sub2api"]
    assert seen == [("cleanup", fake_client), ("validate", fake_client), ("rotate", fake_client)]

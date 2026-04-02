from pathlib import Path

from ops.validate import validate_once


class FakeClient:
    def __init__(self, files: dict[str, dict]):
        self.files = {name: dict(payload) for name, payload in files.items()}
        self.deleted: list[str] = []

    def health_check(self) -> bool:
        return True

    def list_auth_files(self) -> list[dict[str, object]]:
        return [{"name": name} for name in self.files]

    def get_auth_file(self, name: str) -> dict | None:
        payload = self.files.get(name)
        return dict(payload) if isinstance(payload, dict) else None

    def delete_auth_file(self, name: str) -> bool:
        self.deleted.append(name)
        self.files.pop(name, None)
        return True


def _file(email: str, account_id: str) -> dict:
    return {"email": email, "access_token": f"token-{account_id}", "account_id": account_id}


def test_validate_once_scope_used_limits_files_by_management(monkeypatch, tmp_path: Path) -> None:
    client = FakeClient(
        {
            "active@example.com.json": _file("active@example.com", "acct-active"),
            "idle@example.com.json": _file("idle@example.com", "acct-idle"),
        }
    )
    monkeypatch.setattr(
        "ops.validate._fetch_management_auth_files",
        lambda management_base_url, management_key=None: (
            True,
            {
                "active@example.com.json": {"auth_index": "2"},
                "idle@example.com.json": {"auth_index": "9"},
            },
        ),
    )
    monkeypatch.setattr(
        "ops.validate._fetch_used_auth_indexes",
        lambda management_base_url, management_key=None: (True, {"2"}),
    )

    seen: list[str] = []

    def fake_validate_file(path: Path, auth_meta: dict | None):  # type: ignore[no-untyped-def]
        seen.append(path.name)
        assert auth_meta == {"auth_index": "2"}
        return type(
            "FakeEntry",
            (),
            {
                "name": path.name,
                "status_code": 200,
                "action": "keep",
                "detail": "ok",
                "auth_index": "2",
                "account_id": "acct-active",
                "to_dict": lambda self: {
                    "name": path.name,
                    "status_code": 200,
                    "action": "keep",
                    "detail": "ok",
                    "auth_index": "2",
                    "account_id": "acct-active",
                },
            },
        )()

    monkeypatch.setattr("ops.validate._validate_file", fake_validate_file)

    summary = validate_once(client=client, proxy=None, dry_run=True, max_workers=2, scope="used")

    assert summary["validation_limited"] is False
    assert summary["selection_reason"] is None
    assert summary["selected"] == 1
    assert summary["checked"] == 1
    assert seen == ["active@example.com.json"]


def test_validate_once_scope_used_stops_when_management_unavailable(monkeypatch) -> None:
    client = FakeClient({"active@example.com.json": _file("active@example.com", "acct-active")})

    monkeypatch.setattr("ops.validate._fetch_management_auth_files", lambda management_base_url, management_key=None: (False, {}))
    monkeypatch.setattr("ops.validate._fetch_used_auth_indexes", lambda management_base_url, management_key=None: (False, set()))

    summary = validate_once(client=client, proxy=None, dry_run=False, max_workers=2, scope="used")

    assert summary["validation_limited"] is True
    assert summary["selection_reason"] == "management_data_unavailable"
    assert summary["selected"] == 0
    assert summary["checked"] == 0
    assert summary["deleted"] == 0


def test_validate_once_scope_all_does_not_require_management(monkeypatch) -> None:
    client = FakeClient({"active@example.com.json": _file("active@example.com", "acct-1")})

    monkeypatch.setattr(
        "ops.validate._validate_file",
        lambda path, auth_meta: type(
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

    summary = validate_once(client=client, proxy=None, dry_run=False, max_workers=1, scope="all")

    assert summary["validation_limited"] is False
    assert summary["checked"] == 1
    assert summary["deleted"] == 1
    assert client.deleted == ["active@example.com.json"]


def test_validate_once_deletes_pool_backup_together(monkeypatch, tmp_path: Path) -> None:
    client = FakeClient({"active@example.com.json": _file("active@example.com", "acct-1")})
    pool_file = tmp_path / "active@example.com.json"
    pool_file.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        "ops.validate._validate_file",
        lambda path, auth_meta: type(
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

    assert summary["deleted"] == 1
    assert client.deleted == ["active@example.com.json"]
    assert not pool_file.exists()

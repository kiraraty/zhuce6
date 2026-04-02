from __future__ import annotations

import json
from pathlib import Path

from ops.rotate_runtime import _maybe_reconcile_cpa_runtime


class FakeClient:
    def __init__(self) -> None:
        self.files: dict[str, dict[str, object]] = {
            "remote@example.com.json": {
                "email": "remote@example.com",
                "access_token": "remote-token",
                "account_id": "acct-remote",
            }
        }
        self.uploads: list[tuple[str, dict[str, object]]] = []

    def health_check(self) -> bool:
        return True

    def list_auth_files(self) -> list[dict[str, object]]:
        return [{"name": name} for name in self.files]

    def get_auth_file(self, name: str) -> dict[str, object] | None:
        payload = self.files.get(name)
        return dict(payload) if isinstance(payload, dict) else None

    def upload_auth_file(self, name: str, payload: dict[str, object]) -> bool:
        self.uploads.append((name, dict(payload)))
        self.files[name] = dict(payload)
        return True


def test_runtime_reconcile_restores_local_backup_and_cpa_inventory(tmp_path: Path) -> None:
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    local_only = pool_dir / "local@example.com.json"
    local_only.write_text(
        json.dumps(
            {
                "email": "local@example.com",
                "access_token": "local-token",
                "account_id": "acct-local",
            }
        ),
        encoding="utf-8",
    )
    client = FakeClient()

    _maybe_reconcile_cpa_runtime(
        pool_dir=pool_dir,
        management_base_url="http://127.0.0.1:8317/v0/management",
        enabled=True,
        cooldown_seconds=300,
        state_file=tmp_path / "reconcile_state.json",
        restart_enabled=False,
        client=client,
        management_key="secret",
    )

    remote_backup = json.loads((pool_dir / "remote@example.com.json").read_text(encoding="utf-8"))
    local_backup = json.loads(local_only.read_text(encoding="utf-8"))

    assert client.uploads == [
        (
            "local@example.com.json",
            {
                "email": "local@example.com",
                "access_token": "local-token",
                "account_id": "acct-local",
                "health_status": "unknown",
                "source": "register",
                "created_at": "",
                "backup_written": True,
                "cpa_sync_status": "pending",
                "last_cpa_sync_at": "",
                "last_cpa_sync_error": "",
                "last_probe_at": "",
                "last_probe_status_code": None,
                "last_probe_result": "",
                "last_probe_detail": "",
            },
        )
    ]
    assert remote_backup["email"] == "remote@example.com"
    assert remote_backup["backup_written"] is True
    assert remote_backup["cpa_sync_status"] == "synced"
    assert local_backup["cpa_sync_status"] == "synced"

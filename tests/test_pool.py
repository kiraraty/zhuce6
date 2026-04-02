import json
from pathlib import Path

from platforms.chatgpt.pool import load_token_record, update_token_record, write_token_record


def test_write_token_record_uses_single_pool_defaults(tmp_path: Path) -> None:
    path = write_token_record({"email": "user@example.com", "access_token": "tok"}, tmp_path)

    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["email"] == "user@example.com"
    assert payload["source"] == "register"
    assert payload["backup_written"] is True
    assert payload["cpa_sync_status"] == "pending"
    assert "candidate_state" not in payload
    assert "last_recycled_at" not in payload
    assert "in_main_pool" not in payload
    assert "promoted_at" not in payload


def test_load_token_record_backfills_runtime_metadata_without_candidate_fields(tmp_path: Path) -> None:
    path = tmp_path / "user@example.com.json"
    path.write_text(json.dumps({"email": "user@example.com", "access_token": "tok"}), encoding="utf-8")

    payload = load_token_record(path)

    assert payload["backup_written"] is True
    assert payload["cpa_sync_status"] == "pending"
    assert payload["health_status"] == "unknown"
    assert payload["last_probe_result"] == ""
    assert "candidate_state" not in payload
    assert "in_main_pool" not in payload


def test_update_token_record_rewrites_file_atomically(tmp_path: Path) -> None:
    path = tmp_path / "user@example.com.json"
    path.write_text(json.dumps({"email": "user@example.com", "access_token": "tok"}), encoding="utf-8")

    payload = update_token_record(
        path,
        cpa_sync_status="synced",
        last_cpa_sync_at="2026-03-28T12:00:00+08:00",
        health_status="good",
    )

    assert payload["backup_written"] is True
    assert payload["cpa_sync_status"] == "synced"
    assert payload["health_status"] == "good"
    assert payload["last_cpa_sync_at"] == "2026-03-28T12:00:00+08:00"
    assert "in_main_pool" not in payload
    assert not path.with_name(f"{path.name}.tmp").exists()

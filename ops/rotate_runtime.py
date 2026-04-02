"""Runtime reconciliation helpers for rotate."""

from __future__ import annotations

import json
from pathlib import Path

from platforms.chatgpt.pool import load_token_record, now_iso, update_token_record, write_token_record
from .common import CpaClient, DEFAULT_MANAGEMENT_BASE_URL, now


def _reg_entry_names(entries: list[dict] | None) -> set[str]:
    if not isinstance(entries, list):
        return set()
    return {
        str(entry.get("name", "")).strip()
        for entry in entries
        if isinstance(entry, dict) and "@" in str(entry.get("name", "")) and str(entry.get("name", "")).strip()
    }


def _local_pool_names(pool_dir: Path) -> set[str]:
    if not pool_dir.exists():
        return set()
    return {
        path.name
        for path in pool_dir.glob("*.json")
        if path.is_file() and "@" in path.name
    }


def _restore_cpa_from_pool_backups(
    *,
    names: list[str],
    pool_dir: Path,
    backend_client: object,
) -> tuple[int, int]:
    if not hasattr(backend_client, "upload_auth_file"):
        return 0, len(names)
    restored = 0
    failed = 0
    sync_at = now_iso()
    for name in names:
        pool_path = pool_dir / name
        if not pool_path.is_file():
            failed += 1
            continue
        try:
            payload = load_token_record(pool_path)
        except Exception:
            failed += 1
            continue
        if not bool(getattr(backend_client, "upload_auth_file")(name, payload)):
            failed += 1
            update_token_record(
                pool_path,
                backup_written=True,
                cpa_sync_status="failed",
                last_cpa_sync_at=sync_at,
                last_cpa_sync_error="runtime reconcile upload failed",
            )
            continue
        restored += 1
        update_token_record(
            pool_path,
            backup_written=True,
            cpa_sync_status="synced",
            last_cpa_sync_at=sync_at,
            last_cpa_sync_error="",
        )
    return restored, failed


def _restore_pool_backups_from_cpa(
    *,
    names: list[str],
    pool_dir: Path,
    backend_client: object,
) -> tuple[int, int]:
    if not hasattr(backend_client, "get_auth_file"):
        return 0, len(names)
    restored = 0
    failed = 0
    sync_at = now_iso()
    for name in names:
        payload = getattr(backend_client, "get_auth_file")(name)
        if not isinstance(payload, dict):
            failed += 1
            continue
        pool_path = write_token_record(payload, pool_dir, filename=name)
        update_token_record(
            pool_path,
            backup_written=True,
            cpa_sync_status="synced",
            last_cpa_sync_at=sync_at,
            last_cpa_sync_error="",
        )
        restored += 1
    return restored, failed


def _load_runtime_reconcile_state(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_runtime_reconcile_state(path: Path, payload: dict[str, object]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        return


def _fetch_main_pool_entries(
    management_base_url: str = DEFAULT_MANAGEMENT_BASE_URL,
    *,
    client: object | None = None,
    management_key: str | None = None,
) -> list[dict] | None:
    backend_client = client or CpaClient(management_base_url, management_key=management_key)
    if not getattr(backend_client, "health_check")():
        print(f"[{now()}] [rotate] CPA management API 不可达")
        return None
    files = getattr(backend_client, "list_auth_files")()
    return [f for f in files if isinstance(f, dict)]


def _maybe_reconcile_cpa_runtime(
    *,
    pool_dir: Path,
    management_base_url: str,
    enabled: bool,
    cooldown_seconds: int,
    state_file: Path,
    restart_enabled: bool = False,
    client: object | None = None,
    management_key: str | None = None,
) -> None:
    if not enabled:
        return
    backend_client = client or CpaClient(management_base_url, management_key=management_key)

    entries = _fetch_main_pool_entries(
        management_base_url,
        client=backend_client,
        management_key=management_key,
    )
    if entries is None:
        return

    management_names = _reg_entry_names(entries)
    local_names = _local_pool_names(pool_dir)
    if management_names == local_names:
        return

    management_only = sorted(management_names - local_names)
    local_only = sorted(local_names - management_names)
    sample_management_only = ", ".join(management_only[:5]) or "-"
    sample_local_only = ", ".join(local_only[:5]) or "-"
    print(
        f"[{now()}] [rotate] ⚠️ CPA runtime drift detected"
        f" | management={len(management_names)}"
        f" | local_pool={len(local_names)}"
        f" | management_only={len(management_only)} [{sample_management_only}]"
        f" | local_only={len(local_only)} [{sample_local_only}]"
    )

    restored_to_cpa, failed_to_cpa = _restore_cpa_from_pool_backups(
        names=local_only,
        pool_dir=pool_dir,
        backend_client=backend_client,
    )
    restored_to_pool, failed_to_pool = _restore_pool_backups_from_cpa(
        names=management_only,
        pool_dir=pool_dir,
        backend_client=backend_client,
    )

    if restored_to_cpa or restored_to_pool or failed_to_cpa or failed_to_pool:
        print(
            f"[{now()}] [rotate] ↺ runtime reconcile"
            f" | restored_to_cpa={restored_to_cpa}"
            f" | restored_to_pool={restored_to_pool}"
            f" | failed_to_cpa={failed_to_cpa}"
            f" | failed_to_pool={failed_to_pool}"
        )

    _write_runtime_reconcile_state(
        state_file,
        {
            "last_drift_at": now_iso(),
            "management_count": len(management_names),
            "local_pool_count": len(local_names),
            "management_only_sample": sample_management_only,
            "local_only_sample": sample_local_only,
            "restored_to_cpa": restored_to_cpa,
            "restored_to_pool": restored_to_pool,
            "failed_to_cpa": failed_to_cpa,
            "failed_to_pool": failed_to_pool,
            "restart_attempted": False,
            "restart_reason": "api_inventory_local_pool_drift",
            "restart_enabled": bool(restart_enabled),
            "cooldown_seconds": max(0, int(cooldown_seconds)),
        },
    )

    if restart_enabled:
        print(f"[{now()}] [rotate] ⏭️ API-only mode: drift detected but automatic restart has been disabled")

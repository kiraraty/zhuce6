"""Deletion helpers for rotate single-pool mode."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _compact_text(value: str, limit: int = 240) -> str:
    return " ".join(str(value or "").split())[:limit]


def _delete_from_cpa(name: str, client: object | None = None) -> bool:
    if client is None or not hasattr(client, "delete_auth_file"):
        return False
    return bool(getattr(client, "delete_auth_file")(name))


def handle_unhealthy_entries(
    *,
    result: Any,
    reg_entries: list[dict],
    probe_results: dict[str, tuple[int, str, bool]],
    pool_dir: Path,
    backend_client: object | None,
    now_func: Any,
    classify_status_message_func: Any,
    is_deactivated_status_message_func: Any,
) -> set[str]:
    removed_from_main: set[str] = set()
    pool_dir.mkdir(parents=True, exist_ok=True)
    for entry in reg_entries:
        name = str(entry.get("name", "")).strip()
        if not name:
            continue
        status_message = str(entry.get("status_message", ""))
        code = classify_status_message_func(status_message)
        deactivated = is_deactivated_status_message_func(status_message)
        if name in probe_results:
            probe_code, probe_detail, probe_deactivated = probe_results[name]
            if probe_code in {401, 429}:
                code = probe_code
                status_message = probe_detail
                deactivated = probe_deactivated
                if probe_code == 401:
                    probe_label = "deactivated" if deactivated else "401 invalidated"
                    print(f"[{now_func()}] [rotate] 🔎 {name} quota probe → {probe_label}")
                else:
                    print(f"[{now_func()}] [rotate] 🔎 {name} quota probe → 429")
        if code == 401 or deactivated:
            deleted = _delete_from_cpa(name, client=backend_client)
            if not deleted:
                print(f"[{now_func()}] [rotate] ⚠️ {name} 401 删除失败")
                continue
            (pool_dir / name).unlink(missing_ok=True)
            result.deleted_401 += 1
            removed_from_main.add(name)
            print(f"[{now_func()}] [rotate] ❌ {name} 401删除")
            continue
        if code == 429:
            print(f"[{now_func()}] [rotate] ↺ {name} 429保留")
    return removed_from_main

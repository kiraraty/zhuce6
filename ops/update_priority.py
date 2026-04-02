"""Batch update backend auth file priority values."""

from __future__ import annotations

import argparse

from .common import CpaClient, DEFAULT_MANAGEMENT_BASE_URL, now


def read_cpa_file(name: str, client: object) -> dict | None:
    payload = getattr(client, "get_auth_file")(name)
    return payload if isinstance(payload, dict) else None


def write_cpa_file(name: str, payload: dict, client: object) -> bool:
    return bool(getattr(client, "upload_auth_file")(name, payload))


def update_priority_once(
    target_priority: int = 500,
    dry_run: bool = False,
    limit: int | None = None,
    *,
    client: object | None = None,
    management_base_url: str = DEFAULT_MANAGEMENT_BASE_URL,
    management_key: str | None = None,
) -> dict[str, int]:
    backend_client = client or CpaClient(management_base_url, management_key=management_key)
    if not getattr(backend_client, "health_check")():
        return {"total": 0, "need_modify": 0, "modified": 0, "skipped": 0}

    reg_files = sorted(
        str(entry.get("name") or "").strip()
        for entry in getattr(backend_client, "list_auth_files")()
        if "@" in str(entry.get("name") or "").strip() and str(entry.get("name") or "").strip().endswith(".json")
    )
    if limit is not None and limit >= 0:
        reg_files = reg_files[:limit]

    need_modify = 0
    modified = 0
    skipped = 0
    for name in reg_files:
        data = read_cpa_file(name, client=backend_client)
        if data is None:
            skipped += 1
            print(f"[{now()}] [priority] skip {name} read failed")
            continue
        current_priority = data.get("priority", "NOT SET")
        if current_priority == target_priority:
            skipped += 1
            continue
        need_modify += 1
        if dry_run:
            print(f"[{now()}] [priority] dry-run {name}: {current_priority} -> {target_priority}")
            continue
        data["priority"] = target_priority
        if write_cpa_file(name, data, client=backend_client):
            modified += 1
            print(f"[{now()}] [priority] updated {name}: {current_priority} -> {target_priority}")
        else:
            skipped += 1
            print(f"[{now()}] [priority] skip {name} write failed")

    return {
        "total": len(reg_files),
        "need_modify": need_modify,
        "modified": modified,
        "skipped": skipped,
    }


def main() -> None:
    from core.settings import AppSettings

    env_settings = AppSettings.from_env()
    parser = argparse.ArgumentParser(description="Batch update backend auth file priorities for zhuce6")
    parser.add_argument("--dry-run", action="store_true", help="Report changes without writing")
    parser.add_argument("--management-base-url", default=env_settings.cpa_management_base_url or DEFAULT_MANAGEMENT_BASE_URL, help="CPA management base url")
    parser.add_argument("--management-key", default=env_settings.cpa_management_key, help="可选 CPA management key")
    parser.add_argument("--target-priority", type=int, default=500, help="Target priority value")
    parser.add_argument("--limit", type=int, default=None, help="Optional cap for scanned auth files")
    args = parser.parse_args()

    summary = update_priority_once(
        target_priority=args.target_priority,
        dry_run=args.dry_run,
        limit=args.limit,
        management_base_url=args.management_base_url,
        management_key=str(args.management_key or "").strip() or None,
    )
    print(
        f"[{now()}] [priority] total={summary['total']} need_modify={summary['need_modify']}"
        f" modified={summary['modified']} skipped={summary['skipped']} dry_run={args.dry_run}"
    )


if __name__ == "__main__":
    main()

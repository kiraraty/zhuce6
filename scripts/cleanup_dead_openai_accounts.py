#!/usr/bin/env python3
"""Clean up dead OpenAI accounts from sub2api.

Fetches all OpenAI accounts, tries to refresh each token via sub2api,
and deletes accounts whose refresh_token is dead.

Usage:
    python3 scripts/cleanup_dead_openai_accounts.py
    python3 scripts/cleanup_dead_openai_accounts.py --dry-run   # preview only
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

POOL_DIR = Path(__file__).resolve().parent.parent / "pool"
DEAD_DIR = POOL_DIR / "dead"

SUB2API_BASE = os.getenv("ZHUCE6_SUB2API_BASE_URL", "http://43.156.153.114:8080")
SUB2API_EMAIL = os.getenv("ZHUCE6_SUB2API_ADMIN_EMAIL", "soulmate131429@gmail.com")
SUB2API_PASSWORD = os.getenv("ZHUCE6_SUB2API_ADMIN_PASSWORD", "Forever131429@")


def api(method: str, path: str, token: str, body: dict | None = None) -> dict:
    url = f"{SUB2API_BASE}{path}"
    data = json.dumps(body).encode() if body else None
    req = Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=180) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        return {"code": e.code, "message": e.read().decode()[:200]}


def login() -> str:
    body = json.dumps({"email": SUB2API_EMAIL, "password": SUB2API_PASSWORD}).encode()
    req = Request(f"{SUB2API_BASE}/api/v1/auth/login", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    return data["data"]["access_token"]


def get_all_openai_accounts(token: str) -> list[dict]:
    accounts = []
    page = 1
    while True:
        data = api("GET", f"/api/v1/admin/accounts?page={page}&page_size=100", token)
        items = data.get("data", {}).get("items", [])
        if not items:
            break
        for a in items:
            if a.get("platform") == "openai":
                accounts.append(a)
        total = data.get("data", {}).get("total", 0)
        if page * 100 >= total:
            break
        page += 1
    return accounts


def try_refresh(token: str, account_ids: list[int]) -> dict[int, str]:
    """Batch refresh and return {id: error_msg} for failures."""
    if not account_ids:
        return {}
    result = api("POST", "/api/v1/admin/accounts/batch-refresh", token, {"account_ids": account_ids})
    errors = {}
    raw_data = result.get("data") or {}
    raw_errors = raw_data.get("errors") or []
    for e in raw_errors:
        errors[e["account_id"]] = e.get("error", "unknown")
    return errors


def delete_account(token: str, account_id: int) -> bool:
    result = api("DELETE", f"/api/v1/admin/accounts/{account_id}", token)
    return result.get("code") == 0


def main():
    parser = argparse.ArgumentParser(description="Clean up dead OpenAI accounts from sub2api")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, don't delete")
    args = parser.parse_args()

    print("Logging in to sub2api...")
    token = login()
    print("OK")

    print("Fetching all OpenAI accounts...")
    accounts = get_all_openai_accounts(token)
    print(f"Found {len(accounts)} OpenAI accounts")

    if not accounts:
        print("Nothing to do.")
        return

    # Batch refresh to find dead accounts
    all_ids = [a["id"] for a in accounts]
    print(f"Batch refreshing {len(all_ids)} accounts...")

    # Do in batches of 50
    dead_ids: dict[int, str] = {}
    for i in range(0, len(all_ids), 20):
        batch = all_ids[i:i+20]
        errors = try_refresh(token, batch)
        dead_ids.update(errors)
        if errors:
            print(f"  Batch {i//50+1}: {len(errors)} failures")
        else:
            print(f"  Batch {i//50+1}: all OK")
        time.sleep(1)

    alive = len(all_ids) - len(dead_ids)
    print(f"\nResults: {alive} alive, {len(dead_ids)} dead")

    if not dead_ids:
        print("All accounts are healthy!")
        return

    # Build email lookup
    id_to_email = {a["id"]: a.get("credentials", {}).get("email", "?") for a in accounts}

    print(f"\nDead accounts ({len(dead_ids)}):")
    for aid, err in sorted(dead_ids.items()):
        email = id_to_email.get(aid, "?")
        reason = "refresh_token_used" if "already been used" in err else \
                 "account_deactivated" if "deactivated" in err.lower() else \
                 "banned" if "banned" in err.lower() else \
                 "unknown"
        print(f"  id={aid} email={email} reason={reason}")

    if args.dry_run:
        print(f"\n[DRY RUN] Would delete {len(dead_ids)} accounts. Run without --dry-run to proceed.")
        return

    print(f"\nDeleting {len(dead_ids)} dead accounts...")
    DEAD_DIR.mkdir(parents=True, exist_ok=True)
    deleted = 0
    failed = 0
    archived = 0
    for aid in sorted(dead_ids):
        email = id_to_email.get(aid, "?")
        if delete_account(token, aid):
            deleted += 1
            print(f"  Deleted id={aid} ({email})")
        else:
            failed += 1
            print(f"  FAILED to delete id={aid} ({email})")
        # Archive matching local pool files so upload won't re-upload them.
        if email and email != "?":
            for f in POOL_DIR.glob(f"{email}.json"):
                try:
                    f.rename(DEAD_DIR / f.name)
                    archived += 1
                except OSError:
                    pass
        time.sleep(0.2)

    print(f"\nDone: {deleted} deleted, {failed} failed, {alive} alive remaining, {archived} local files archived to pool/dead/")


if __name__ == "__main__":
    main()

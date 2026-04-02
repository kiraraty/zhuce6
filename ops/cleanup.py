"""Clean expired registration tokens from backend auth storage."""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path

from curl_cffi import requests

from .common import CpaClient, DEFAULT_MANAGEMENT_BASE_URL, DEFAULT_POOL_DIR, now


def is_expired(data: dict) -> bool:
    expired_str = str(data.get("expired") or "").strip()
    if not expired_str:
        return False
    try:
        expired_at = datetime.fromisoformat(expired_str.replace("Z", "+00:00"))
    except ValueError:
        return False
    return expired_at < datetime.now(timezone.utc)


def try_refresh(refresh_token: str, proxy: str | None) -> bool:
    try:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        response = requests.post(
            "https://auth0.openai.com/oauth/token",
            json={
                "redirect_uri": "com.openai.chat://auth0.openai.com/ios/com.openai.chat/callback",
                "grant_type": "refresh_token",
                "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
                "refresh_token": refresh_token,
            },
            proxies=proxies,
            impersonate="chrome",
            timeout=15,
        )
        return response.status_code == 200 and bool(response.json().get("access_token"))
    except Exception:
        return False


def _hard_delete_pool_file(pool_dir: Path, name: str, reason: str) -> None:
    pool_file = pool_dir / name
    if not pool_file.exists():
        return
    pool_file.unlink(missing_ok=True)
    print(f"[{now()}] [清理] ❌ pool {name} deleted ({reason})")


def cleanup_once(
    proxy: str | None = None,
    pool_dir: Path = DEFAULT_POOL_DIR,
    *,
    client: object | None = None,
    management_base_url: str = DEFAULT_MANAGEMENT_BASE_URL,
    management_key: str | None = None,
) -> tuple[int, int, int]:
    backend_client = client or CpaClient(management_base_url, management_key=management_key)
    if not getattr(backend_client, "health_check")():
        return 0, 0, 0

    reg_files = sorted(
        str(entry.get("name") or "").strip()
        for entry in getattr(backend_client, "list_auth_files")()
        if "@" in str(entry.get("name") or "").strip()
    )

    checked = 0
    deleted = 0
    refreshed = 0
    for name in reg_files:
        checked += 1
        data = getattr(backend_client, "get_auth_file")(name)
        if not isinstance(data, dict):
            continue
        refresh_token = str(data.get("refresh_token") or "").strip()
        if not refresh_token:
            print(f"[{now()}] [清理] ⚠️ {name} 无 refresh_token, 删除")
            if getattr(backend_client, "delete_auth_file")(name):
                deleted += 1
                _hard_delete_pool_file(pool_dir, name, "no_refresh_token")
            continue
        if not is_expired(data):
            continue
        if try_refresh(refresh_token, proxy):
            refreshed += 1
            print(f"[{now()}] [清理] 🔄 {name} 已过期但刷新成功, 保留")
            continue
        print(f"[{now()}] [清理] ❌ {name} 已过期且刷新失败, 删除")
        if getattr(backend_client, "delete_auth_file")(name):
            deleted += 1
            _hard_delete_pool_file(pool_dir, name, "expired_refresh_failed")

    return checked, deleted, refreshed


def main() -> None:
    from core.settings import AppSettings

    env_settings = AppSettings.from_env()
    parser = argparse.ArgumentParser(description="清理 zhuce6 backend 中失效 token")
    parser.add_argument("--interval", type=int, default=300, help="清理间隔秒数")
    parser.add_argument("--proxy", default=None, help="可选代理地址")
    parser.add_argument("--once", action="store_true", help="只执行一轮")
    parser.add_argument("--management-base-url", default=env_settings.cpa_management_base_url or DEFAULT_MANAGEMENT_BASE_URL, help="CPA management base url")
    parser.add_argument("--management-key", default=env_settings.cpa_management_key, help="可选 CPA management key")
    parser.add_argument("--pool-dir", default=str(env_settings.pool_dir or DEFAULT_POOL_DIR), help="本地 pool 目录")
    args = parser.parse_args()

    interval = max(1, args.interval)
    proxy = str(args.proxy or "").strip() or None
    pool_dir = Path(args.pool_dir).expanduser().resolve()

    print(
        "[清理] 启动"
        f" | management_base_url: {args.management_base_url}"
        f" | 间隔: {interval}s"
        f" | proxy: {proxy or 'none'}"
    )

    while True:
        cycle_started_at = time.time()
        try:
            checked, deleted, refreshed = cleanup_once(
                proxy,
                pool_dir,
                management_base_url=args.management_base_url,
                management_key=str(args.management_key or "").strip() or None,
            )
            elapsed = time.time() - cycle_started_at
            if checked > 0 or deleted > 0 or refreshed > 0:
                print(f"[{now()}] [清理] 本轮: 检查 {checked}, 删除 {deleted}, 刷新验证 {refreshed}")
            print(f"[{now()}] [清理] 本轮耗时: {elapsed:.2f}s")
        except Exception as exc:
            print(f"[{now()}] [错误] 清理异常: {exc}")
            elapsed = time.time() - cycle_started_at

        if args.once:
            break
        time.sleep(max(0, interval - elapsed))


if __name__ == "__main__":
    main()

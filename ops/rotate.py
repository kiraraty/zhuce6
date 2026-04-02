"""Single-pool rotation: probe main pool and hard-delete unhealthy accounts."""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from pathlib import Path

from core.settings import AppSettings
from .common import CpaClient, DEFAULT_MANAGEMENT_BASE_URL, DEFAULT_POOL_DIR, now
from .rotate_probe import _collect_quota_probe_results, classify_status_message
from .rotate_promote import handle_unhealthy_entries
from .rotate_runtime import _fetch_main_pool_entries, _maybe_reconcile_cpa_runtime


@dataclass
class RotateResult:
    main_pool_before: int = 0
    main_pool_after: int = 0
    deleted_401: int = 0
    deleted_429: int = 0
    quota_probed: int = 0
    quota_probe_401: int = 0
    quota_probe_429: int = 0
    quota_probe_skipped: int = 0


def rotate_once(
    pool_dir: Path,
    *,
    client: object | None = None,
    management_base_url: str = DEFAULT_MANAGEMENT_BASE_URL,
    cpa_management_key: str | None = None,
    rotate_probe_workers: int = 8,
    cpa_runtime_reconcile_enabled: bool = True,
    cpa_runtime_reconcile_cooldown_seconds: int = 300,
    cpa_runtime_reconcile_restart_enabled: bool = False,
) -> RotateResult:
    result = RotateResult()
    backend_client = client or CpaClient(management_base_url, management_key=cpa_management_key)
    if not getattr(backend_client, "health_check")():
        return result

    pool_dir = Path(pool_dir).expanduser().resolve()
    pool_dir.mkdir(parents=True, exist_ok=True)

    entries = _fetch_main_pool_entries(
        management_base_url,
        client=backend_client,
        management_key=cpa_management_key,
    )
    if entries is None:
        return result

    reg_entries = [entry for entry in entries if "@" in str(entry.get("name", ""))]
    result.main_pool_before = len(reg_entries)

    management_key = None
    if isinstance(backend_client, CpaClient):
        management_key = backend_client._resolve_key()  # noqa: SLF001

    probe_results: dict[str, tuple[int, str, bool]] = {}
    if management_key:
        initial_classified = []
        for entry in reg_entries:
            status_message = str(entry.get("status_message", ""))
            classified_code = classify_status_message(status_message)
            if classified_code in {401, 429}:
                continue
            initial_classified.append(entry)
        probe_results, probe_counters = _collect_quota_probe_results(
            initial_classified,
            management_key=management_key,
            management_base_url=management_base_url,
            max_count=0,
            workers=rotate_probe_workers,
        )
        result.quota_probed = probe_counters["probed"]
        result.quota_probe_401 = probe_counters["probe_401"]
        result.quota_probe_429 = probe_counters["probe_429"]
        result.quota_probe_skipped = probe_counters["probe_skipped"]

    handle_unhealthy_entries(
        result=result,
        reg_entries=reg_entries,
        probe_results=probe_results,
        pool_dir=pool_dir,
        backend_client=backend_client,
        now_func=now,
        classify_status_message_func=classify_status_message,
        is_deactivated_status_message_func=lambda message: "deactivated" in str(message or "").lower(),
    )

    result.main_pool_after = result.main_pool_before - result.deleted_401 - result.deleted_429
    _maybe_reconcile_cpa_runtime(
        pool_dir=pool_dir,
        management_base_url=management_base_url,
        enabled=cpa_runtime_reconcile_enabled,
        cooldown_seconds=cpa_runtime_reconcile_cooldown_seconds,
        restart_enabled=cpa_runtime_reconcile_restart_enabled,
        state_file=pool_dir / "cpa_runtime_reconcile_state.json",
        client=backend_client,
        management_key=cpa_management_key,
    )
    return result


def print_rotate_summary(r: RotateResult) -> None:
    print(
        f"[{now()}] [rotate] summary"
        f" | 主池: {r.main_pool_before} → {r.main_pool_after}"
        f" | 401删除: {r.deleted_401}"
        f" | quota探测: {r.quota_probed}"
        f" | probe401: {r.quota_probe_401}"
        f" | probe429: {r.quota_probe_429}"
        f" | probe跳过: {r.quota_probe_skipped}"
    )


def main() -> None:
    env_settings = AppSettings.from_env()
    parser = argparse.ArgumentParser(description="Rotate zhuce6 backend main pool in single-pool mode")
    parser.add_argument("--pool-dir", default=str(env_settings.pool_dir or DEFAULT_POOL_DIR), help="本地 pool 目录")
    parser.add_argument("--interval", type=int, default=env_settings.rotate_interval, help="轮换间隔秒数")
    parser.add_argument("--once", action="store_true", help="只执行一轮")
    parser.add_argument("--management-base-url", default=env_settings.cpa_management_base_url or DEFAULT_MANAGEMENT_BASE_URL, help="CPA management base url")
    parser.add_argument("--management-key", default=env_settings.cpa_management_key, help="可选 CPA management key")
    parser.add_argument("--rotate-probe-workers", type=int, default=env_settings.rotate_probe_workers, help="quota probe 并发数")
    parser.add_argument("--cpa-runtime-reconcile-enabled", action="store_true" if not env_settings.cpa_runtime_reconcile_enabled else "store_false", default=env_settings.cpa_runtime_reconcile_enabled, help="是否启用 CPA runtime drift 检测")
    parser.add_argument("--cpa-runtime-reconcile-cooldown-seconds", type=int, default=env_settings.cpa_runtime_reconcile_cooldown_seconds, help="CPA runtime drift 观测 cooldown")
    parser.add_argument("--cpa-runtime-reconcile-restart-enabled", action="store_true" if not env_settings.cpa_runtime_reconcile_restart_enabled else "store_false", default=env_settings.cpa_runtime_reconcile_restart_enabled, help="保留兼容字段, API-only 模式下不会自动重启")
    args = parser.parse_args()

    interval = max(1, args.interval)
    pool_dir = Path(args.pool_dir).expanduser().resolve()
    print(
        "[rotate] 启动"
        f" | pool: {pool_dir}"
        f" | management_base_url: {args.management_base_url}"
        f" | quota probe workers: {max(1, int(args.rotate_probe_workers))}"
        f" | interval: {interval}s"
    )

    while True:
        started = time.time()
        result = rotate_once(
            pool_dir=pool_dir,
            management_base_url=str(args.management_base_url or "").strip() or DEFAULT_MANAGEMENT_BASE_URL,
            cpa_management_key=str(args.management_key or "").strip() or None,
            rotate_probe_workers=max(1, int(args.rotate_probe_workers)),
            cpa_runtime_reconcile_enabled=bool(args.cpa_runtime_reconcile_enabled),
            cpa_runtime_reconcile_cooldown_seconds=max(0, int(args.cpa_runtime_reconcile_cooldown_seconds)),
            cpa_runtime_reconcile_restart_enabled=bool(args.cpa_runtime_reconcile_restart_enabled),
        )
        print_rotate_summary(result)
        elapsed = time.time() - started
        if args.once:
            break
        time.sleep(max(0, interval - elapsed))


if __name__ == "__main__":
    main()

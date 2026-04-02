"""Runtime settings for zhuce6."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from .env_loader import bootstrap_env

bootstrap_env()

from ops.common import DEFAULT_POOL_DIR
from .paths import (
    DEFAULT_ACCOUNT_SURVIVAL_STATE_FILE,
    DEFAULT_DASHBOARD_LOG_FILE,
    DEFAULT_ENV_FILE,
    LOG_DIR,
    PROJECT_ROOT,
    DEFAULT_RESPONSES_SURVIVAL_STATE_FILE,
    DEFAULT_RUNTIME_STATE_FILE,
    CONFIG_DIR,
    STATE_DIR,
)


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, str(default))).strip().lower()
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AppSettings:
    runtime_mode: str = "full"
    host: str = "127.0.0.1"
    port: int = 8000
    project_root: Path = PROJECT_ROOT
    config_dir: Path = CONFIG_DIR
    state_dir: Path = STATE_DIR
    log_dir: Path = LOG_DIR
    env_file: Path = DEFAULT_ENV_FILE
    cleanup_enabled: bool = True
    validate_enabled: bool = True
    cleanup_interval: int = 300
    validate_interval: int = 180
    d1_cleanup_enabled: bool = True
    d1_cleanup_interval: int = 1800
    d1_database_id: str = ""
    d1_mail_retention_hours: int = 2
    d1_address_retention_hours: int = 24
    pool_dir: Path = DEFAULT_POOL_DIR
    cleanup_proxy: str | None = None
    validate_proxy: str | None = None
    validate_scope: str = "all"
    cpa_management_base_url: str = "http://127.0.0.1:8317/v0/management"
    cpa_management_key: str | None = None
    backend: str = "cpa"
    sub2api_base_url: str = "http://127.0.0.1:8080"
    sub2api_admin_email: str = ""
    sub2api_admin_password: str = ""
    sub2api_api_key: str = ""
    validate_max_workers: int = 8
    rotate_enabled: bool = True
    rotate_interval: int = 300
    rotate_probe_workers: int = 8
    # Registration loop settings
    register_enabled: bool = False
    register_threads: int = 8
    register_interval: int = 5
    register_proxy: str | None = "http://127.0.0.1:7899"
    register_mail_provider: str = "cfmail"
    register_sleep_min: int = 3
    register_sleep_max: int = 10
    register_target_count: int = 0  # 0 = unlimited
    register_batch_threads: int = 1
    register_batch_target_count: int = 20
    register_batch_interval_seconds: int = 10800
    register_max_consecutive_failures: int = 3
    register_log_file: str = str(LOG_DIR / "register.log")
    dashboard_log_file: str = str(DEFAULT_DASHBOARD_LOG_FILE)
    dashboard_allowed_origins: tuple[str, ...] = ()
    enable_proxy_pool: bool = True
    proxy_pool_config: Path | None = PROJECT_ROOT / "clash_config.yaml"
    proxy_pool_direct_urls: str = ""
    proxy_pool_regions: tuple[str, ...] = ("jp", "tw", "hk", "sg")
    proxy_pool_size: int = 20
    proxy_pool_exclude_names: tuple[str, ...] = ()
    proxy_pool_preferred_patterns: tuple[str, ...] = ()
    runtime_state_file: Path = DEFAULT_RUNTIME_STATE_FILE
    recycle_rewarm_cooldown_seconds: int = 1800  # DEPRECATED: unused after single-pool refactor
    cpa_runtime_reconcile_enabled: bool = True
    cpa_runtime_reconcile_cooldown_seconds: int = 300
    cpa_runtime_reconcile_restart_enabled: bool = False
    account_survival_enabled: bool = True
    account_survival_interval: int = 120
    account_survival_cohort_size: int = 10
    account_survival_proxy: str | None = None
    account_survival_timeout_seconds: int = 15
    account_survival_state_file: Path = DEFAULT_ACCOUNT_SURVIVAL_STATE_FILE
    responses_survival_state_file: Path = DEFAULT_RESPONSES_SURVIVAL_STATE_FILE
    cfmail_rotation_window: int = 10
    cfmail_rotation_blacklist_threshold: int = 6
    cfmail_rotation_max_successes: int = 2
    cfmail_api_token: str = ""

    @property
    def proxy_pool_configured(self) -> bool:
        return bool(self.enable_proxy_pool and (self.proxy_pool_config or self.proxy_pool_direct_urls.strip()))

    def validate_cfmail_env(self) -> list[str]:
        missing: list[str] = []
        token = str(os.getenv("ZHUCE6_CFMAIL_API_TOKEN", "")).strip()
        auth_email = str(os.getenv("ZHUCE6_CFMAIL_CF_AUTH_EMAIL", "")).strip()
        auth_key = str(os.getenv("ZHUCE6_CFMAIL_CF_AUTH_KEY", "")).strip()
        if not token and not (auth_email and auth_key):
            missing.append("ZHUCE6_CFMAIL_API_TOKEN")
        for key in (
            "ZHUCE6_CFMAIL_CF_ACCOUNT_ID",
            "ZHUCE6_CFMAIL_CF_ZONE_ID",
            "ZHUCE6_CFMAIL_WORKER_NAME",
            "ZHUCE6_CFMAIL_ZONE_NAME",
        ):
            if not os.getenv(key, "").strip():
                missing.append(key)
        return missing

    @classmethod
    def from_env(cls) -> "AppSettings":
        bootstrap_env()
        project_root = Path(
            str(os.getenv("ZHUCE6_PROJECT_ROOT", str(PROJECT_ROOT))).strip() or str(PROJECT_ROOT)
        ).expanduser().resolve()
        config_dir = Path(
            str(os.getenv("ZHUCE6_CONFIG_DIR", str(project_root / "config"))).strip() or str(project_root / "config")
        ).expanduser().resolve()
        state_dir = Path(
            str(os.getenv("ZHUCE6_STATE_DIR", str(project_root / "state"))).strip() or str(project_root / "state")
        ).expanduser().resolve()
        log_dir = Path(
            str(os.getenv("ZHUCE6_LOG_DIR", str(project_root / "logs"))).strip() or str(project_root / "logs")
        ).expanduser().resolve()
        pool_dir = Path(
            str(os.getenv("ZHUCE6_POOL_DIR", str(project_root / "pool"))).strip() or str(project_root / "pool")
        ).expanduser().resolve()
        env_file = Path(
            str(os.getenv("ZHUCE6_ENV_FILE", str(project_root / ".env"))).strip() or str(project_root / ".env")
        ).expanduser().resolve()
        runtime_state_file = Path(
            str(os.getenv("ZHUCE6_RUNTIME_STATE_FILE", str(state_dir / "runtime_state.json"))).strip()
            or str(state_dir / "runtime_state.json")
        ).expanduser().resolve()
        account_survival_state_file = Path(
            str(os.getenv("ZHUCE6_ACCOUNT_SURVIVAL_STATE_FILE", str(state_dir / "account_survival_tracker.json"))).strip()
            or str(state_dir / "account_survival_tracker.json")
        ).expanduser().resolve()
        responses_survival_state_file = Path(
            str(os.getenv("ZHUCE6_RESPONSES_SURVIVAL_STATE_FILE", str(state_dir / "responses_survival_tracker.json"))).strip()
            or str(state_dir / "responses_survival_tracker.json")
        ).expanduser().resolve()
        register_log_file = str(
            os.getenv("ZHUCE6_REGISTER_LOG_FILE", str(log_dir / "register.log")).strip() or str(log_dir / "register.log")
        )
        dashboard_log_file = str(
            os.getenv("ZHUCE6_DASHBOARD_LOG_FILE", str(log_dir / "dashboard.log")).strip() or str(log_dir / "dashboard.log")
        )
        dashboard_allowed_origins = tuple(
            part.strip().rstrip("/")
            for part in str(os.getenv("ZHUCE6_DASHBOARD_ALLOWED_ORIGINS", "")).split(",")
            if part.strip()
        )
        cleanup_proxy = str(os.getenv("ZHUCE6_CLEANUP_PROXY", "")).strip() or None
        validate_proxy = str(os.getenv("ZHUCE6_VALIDATE_PROXY", "")).strip() or None
        cpa_management_key = str(os.getenv("ZHUCE6_CPA_MANAGEMENT_KEY", "")).strip() or None
        register_proxy = str(os.getenv("ZHUCE6_REGISTER_PROXY", "http://127.0.0.1:7899")).strip() or "http://127.0.0.1:7899"
        account_survival_proxy = (
            str(os.getenv("ZHUCE6_ACCOUNT_SURVIVAL_PROXY", "")).strip()
            or validate_proxy
            or register_proxy
        )
        validate_scope = str(os.getenv("ZHUCE6_VALIDATE_SCOPE", "all")).strip().lower() or "all"
        if validate_scope not in {"used", "all"}:
            validate_scope = "all"
        proxy_pool_config_raw = str(os.getenv("ZHUCE6_PROXY_POOL_CONFIG", str(project_root / "clash_config.yaml"))).strip()
        proxy_pool_direct_urls = str(os.getenv("ZHUCE6_PROXY_POOL_DIRECT_URLS", "")).strip()
        proxy_pool_regions = tuple(
            part.strip().lower()
            for part in str(os.getenv("ZHUCE6_PROXY_POOL_REGIONS", "jp,tw,hk,sg")).split(",")
            if part.strip()
        ) or ("jp", "tw", "hk", "sg")
        proxy_pool_exclude_names = tuple(
            part.strip()
            for part in str(os.getenv("ZHUCE6_PROXY_POOL_EXCLUDE_NAMES", "")).split(",")
            if part.strip()
        )
        proxy_pool_preferred_patterns = tuple(
            part.strip()
            for part in str(os.getenv("ZHUCE6_PROXY_POOL_PREFERRED_PATTERNS", "")).split(",")
            if part.strip()
        )
        for directory in (config_dir, state_dir, log_dir, pool_dir):
            directory.mkdir(parents=True, exist_ok=True)
        runtime_state_file.parent.mkdir(parents=True, exist_ok=True)
        account_survival_state_file.parent.mkdir(parents=True, exist_ok=True)
        responses_survival_state_file.parent.mkdir(parents=True, exist_ok=True)

        return cls(
            runtime_mode=str(os.getenv("ZHUCE6_RUNTIME_MODE", "full")).strip() or "full",
            host=str(os.getenv("ZHUCE6_HOST", "127.0.0.1")).strip() or "127.0.0.1",
            port=max(1, int(os.getenv("ZHUCE6_DASHBOARD_PORT", os.getenv("ZHUCE6_PORT", "8000")))),
            project_root=project_root,
            config_dir=config_dir,
            state_dir=state_dir,
            log_dir=log_dir,
            env_file=env_file,
            cleanup_enabled=_env_bool("ZHUCE6_CLEANUP_ENABLED", True),
            validate_enabled=_env_bool("ZHUCE6_VALIDATE_ENABLED", True),
            cleanup_interval=max(1, int(os.getenv("ZHUCE6_CLEANUP_INTERVAL", "300"))),
            validate_interval=max(1, int(os.getenv("ZHUCE6_VALIDATE_INTERVAL", "180"))),
            d1_cleanup_enabled=_env_bool("ZHUCE6_D1_CLEANUP_ENABLED", True),
            d1_cleanup_interval=max(1, int(os.getenv("ZHUCE6_D1_CLEANUP_INTERVAL", "1800"))),
            d1_database_id=(
                str(os.getenv("ZHUCE6_D1_DATABASE_ID", "")).strip()
            ),
            d1_mail_retention_hours=max(0, int(os.getenv("ZHUCE6_D1_MAIL_RETENTION_HOURS", "2"))),
            d1_address_retention_hours=max(0, int(os.getenv("ZHUCE6_D1_ADDRESS_RETENTION_HOURS", "24"))),
            pool_dir=pool_dir,
            cleanup_proxy=cleanup_proxy,
            validate_proxy=validate_proxy,
            validate_scope=validate_scope,
            cpa_management_base_url=str(
                os.getenv("ZHUCE6_CPA_MANAGEMENT_BASE_URL", "http://127.0.0.1:8317/v0/management")
            ).strip()
            or "http://127.0.0.1:8317/v0/management",
            cpa_management_key=cpa_management_key,
            backend=(str(os.getenv("ZHUCE6_BACKEND", "cpa")).strip().lower() or "cpa"),
            sub2api_base_url=str(os.getenv("ZHUCE6_SUB2API_BASE_URL", "http://127.0.0.1:8080")).strip() or "http://127.0.0.1:8080",
            sub2api_admin_email=str(os.getenv("ZHUCE6_SUB2API_ADMIN_EMAIL", "")).strip(),
            sub2api_admin_password=str(os.getenv("ZHUCE6_SUB2API_ADMIN_PASSWORD", "")).strip(),
            sub2api_api_key=str(os.getenv("ZHUCE6_SUB2API_API_KEY", "")).strip(),
            validate_max_workers=max(1, int(os.getenv("ZHUCE6_VALIDATE_MAX_WORKERS", "8"))),
            rotate_enabled=_env_bool("ZHUCE6_ROTATE_ENABLED", True),
            rotate_interval=max(1, int(os.getenv("ZHUCE6_ROTATE_INTERVAL", "120"))),
            rotate_probe_workers=max(1, int(os.getenv("ZHUCE6_ROTATE_PROBE_WORKERS", "8"))),
            register_enabled=_env_bool("ZHUCE6_REGISTER_ENABLED", False),
            register_threads=max(1, int(os.getenv("ZHUCE6_REGISTER_THREADS", "8"))),
            register_interval=max(1, int(os.getenv("ZHUCE6_REGISTER_INTERVAL", "5"))),
            register_proxy=register_proxy,
            register_mail_provider=str(os.getenv("ZHUCE6_REGISTER_MAIL_PROVIDER", "cfmail")).strip() or "cfmail",
            register_sleep_min=max(1, int(os.getenv("ZHUCE6_REGISTER_SLEEP_MIN", "3"))),
            register_sleep_max=max(1, int(os.getenv("ZHUCE6_REGISTER_SLEEP_MAX", "10"))),
            register_target_count=max(0, int(os.getenv("ZHUCE6_REGISTER_TARGET_COUNT", "0"))),
            register_batch_threads=max(1, int(os.getenv("ZHUCE6_REGISTER_BATCH_THREADS", "1"))),
            register_batch_target_count=max(1, int(os.getenv("ZHUCE6_REGISTER_BATCH_TARGET_COUNT", "20"))),
            register_batch_interval_seconds=max(60, int(os.getenv("ZHUCE6_REGISTER_BATCH_INTERVAL_SECONDS", "10800"))),
            register_max_consecutive_failures=max(1, int(os.getenv("ZHUCE6_REGISTER_MAX_CONSECUTIVE_FAILURES", "3"))),
            register_log_file=register_log_file,
            dashboard_log_file=dashboard_log_file,
            dashboard_allowed_origins=dashboard_allowed_origins,
            enable_proxy_pool=_env_bool("ZHUCE6_ENABLE_PROXY_POOL", True),
            proxy_pool_config=Path(proxy_pool_config_raw).expanduser().resolve() if proxy_pool_config_raw else None,
            proxy_pool_direct_urls=proxy_pool_direct_urls,
            proxy_pool_regions=proxy_pool_regions,
            proxy_pool_size=max(1, int(os.getenv("ZHUCE6_PROXY_POOL_SIZE", "20"))),
            proxy_pool_exclude_names=proxy_pool_exclude_names,
            proxy_pool_preferred_patterns=proxy_pool_preferred_patterns,
            runtime_state_file=runtime_state_file,
            recycle_rewarm_cooldown_seconds=max(
                0,
                int(os.getenv("ZHUCE6_RECYCLE_REWARM_COOLDOWN_SECONDS", "1800")),
            ),
            cpa_runtime_reconcile_enabled=_env_bool("ZHUCE6_CPA_RUNTIME_RECONCILE_ENABLED", True),
            cpa_runtime_reconcile_cooldown_seconds=max(
                0,
                int(os.getenv("ZHUCE6_CPA_RUNTIME_RECONCILE_COOLDOWN_SECONDS", "300")),
            ),
            cpa_runtime_reconcile_restart_enabled=_env_bool("ZHUCE6_CPA_RUNTIME_RECONCILE_RESTART_ENABLED", False),
            account_survival_enabled=_env_bool("ZHUCE6_ACCOUNT_SURVIVAL_ENABLED", True),
            account_survival_interval=max(30, int(os.getenv("ZHUCE6_ACCOUNT_SURVIVAL_INTERVAL", "120"))),
            account_survival_cohort_size=max(1, int(os.getenv("ZHUCE6_ACCOUNT_SURVIVAL_COHORT_SIZE", "10"))),
            account_survival_proxy=account_survival_proxy or None,
            account_survival_timeout_seconds=max(5, int(os.getenv("ZHUCE6_ACCOUNT_SURVIVAL_TIMEOUT_SECONDS", "15"))),
            account_survival_state_file=account_survival_state_file,
            responses_survival_state_file=responses_survival_state_file,
            cfmail_rotation_window=max(1, int(os.getenv("ZHUCE6_CFMAIL_ROTATION_WINDOW", "10"))),
            cfmail_rotation_blacklist_threshold=max(1, int(os.getenv("ZHUCE6_CFMAIL_ROTATION_BLACKLIST_THRESHOLD", "6"))),
            cfmail_rotation_max_successes=max(0, int(os.getenv("ZHUCE6_CFMAIL_ROTATION_MAX_SUCCESSES", "2"))),
            cfmail_api_token=str(os.getenv("ZHUCE6_CFMAIL_API_TOKEN", "")).strip(),
        )

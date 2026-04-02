from core.settings import AppSettings


def test_app_settings_validate_scope_defaults_to_all(monkeypatch) -> None:
    monkeypatch.delenv("ZHUCE6_VALIDATE_SCOPE", raising=False)

    settings = AppSettings.from_env()

    assert settings.validate_scope == "all"



def test_app_settings_rejects_legacy_validate_scope(monkeypatch) -> None:
    monkeypatch.setenv("ZHUCE6_VALIDATE_SCOPE", "legacy_invalid_scope")

    settings = AppSettings.from_env()

    assert settings.validate_scope == "all"



def test_app_settings_account_survival_default_cohort_is_ten(monkeypatch) -> None:
    monkeypatch.delenv("ZHUCE6_ACCOUNT_SURVIVAL_COHORT_SIZE", raising=False)

    settings = AppSettings.from_env()

    assert settings.account_survival_cohort_size == 10



def test_app_settings_d1_cleanup_defaults(monkeypatch) -> None:
    monkeypatch.delenv("ZHUCE6_D1_CLEANUP_ENABLED", raising=False)
    monkeypatch.delenv("ZHUCE6_D1_CLEANUP_INTERVAL", raising=False)
    monkeypatch.delenv("ZHUCE6_D1_DATABASE_ID", raising=False)
    monkeypatch.delenv("ZHUCE6_D1_MAIL_RETENTION_HOURS", raising=False)
    monkeypatch.delenv("ZHUCE6_D1_ADDRESS_RETENTION_HOURS", raising=False)

    settings = AppSettings.from_env()

    assert settings.d1_cleanup_enabled is True
    assert settings.d1_cleanup_interval == 1800
    assert settings.d1_database_id == ""
    assert settings.d1_mail_retention_hours == 2
    assert settings.d1_address_retention_hours == 24



def test_app_settings_d1_cleanup_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("ZHUCE6_D1_CLEANUP_ENABLED", "false")
    monkeypatch.setenv("ZHUCE6_D1_CLEANUP_INTERVAL", "900")
    monkeypatch.setenv("ZHUCE6_D1_DATABASE_ID", "db-test")
    monkeypatch.setenv("ZHUCE6_D1_MAIL_RETENTION_HOURS", "6")
    monkeypatch.setenv("ZHUCE6_D1_ADDRESS_RETENTION_HOURS", "48")

    settings = AppSettings.from_env()

    assert settings.d1_cleanup_enabled is False
    assert settings.d1_cleanup_interval == 900
    assert settings.d1_database_id == "db-test"
    assert settings.d1_mail_retention_hours == 6
    assert settings.d1_address_retention_hours == 48



def test_app_settings_reads_proxy_pool_direct_urls(monkeypatch) -> None:
    monkeypatch.setenv("ZHUCE6_PROXY_POOL_DIRECT_URLS", "socks5://1.2.3.4:1080;http://5.6.7.8:8080")

    settings = AppSettings.from_env()

    assert settings.proxy_pool_direct_urls == "socks5://1.2.3.4:1080;http://5.6.7.8:8080"



def test_app_settings_matches_shell_default_values(monkeypatch, tmp_path) -> None:
    keys = [
        "ZHUCE6_CONFIG_DIR",
        "ZHUCE6_STATE_DIR",
        "ZHUCE6_LOG_DIR",
        "ZHUCE6_POOL_DIR",
        "ZHUCE6_REGISTER_LOG_FILE",
        "ZHUCE6_RUNTIME_STATE_FILE",
        "ZHUCE6_ACCOUNT_SURVIVAL_STATE_FILE",
        "ZHUCE6_RESPONSES_SURVIVAL_STATE_FILE",
        "ZHUCE6_ROTATE_INTERVAL",
        "ZHUCE6_CPA_RUNTIME_RECONCILE_ENABLED",
        "ZHUCE6_CPA_RUNTIME_RECONCILE_COOLDOWN_SECONDS",
        "ZHUCE6_CPA_RUNTIME_RECONCILE_RESTART_ENABLED",
        "ZHUCE6_REGISTER_PROXY",
        "ZHUCE6_REGISTER_MAIL_PROVIDER",
        "ZHUCE6_REGISTER_BATCH_THREADS",
        "ZHUCE6_REGISTER_BATCH_TARGET_COUNT",
        "ZHUCE6_REGISTER_BATCH_INTERVAL_SECONDS",
        "ZHUCE6_ACCOUNT_SURVIVAL_ENABLED",
        "ZHUCE6_DASHBOARD_ALLOWED_ORIGINS",
        "ZHUCE6_ENABLE_PROXY_POOL",
        "ZHUCE6_PROXY_POOL_CONFIG",
        "ZHUCE6_PROXY_POOL_SIZE",
        "ZHUCE6_PROXY_POOL_REGIONS",
        "ZHUCE6_CFMAIL_ROTATION_WINDOW",
        "ZHUCE6_CFMAIL_ROTATION_BLACKLIST_THRESHOLD",
        "ZHUCE6_CFMAIL_ROTATION_MAX_SUCCESSES",
        "ZHUCE6_ACCOUNT_SURVIVAL_PROXY",
        "ZHUCE6_VALIDATE_SCOPE",
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ZHUCE6_PROJECT_ROOT", str(tmp_path))

    settings = AppSettings.from_env()

    assert settings.rotate_interval == 120
    assert settings.recycle_rewarm_cooldown_seconds == 1800
    assert settings.cpa_runtime_reconcile_enabled is True
    assert settings.cpa_runtime_reconcile_cooldown_seconds == 300
    assert settings.cpa_runtime_reconcile_restart_enabled is False
    assert settings.register_proxy == "http://127.0.0.1:7899"
    assert settings.register_mail_provider == "cfmail"
    assert settings.register_batch_threads == 1
    assert settings.register_batch_target_count == 20
    assert settings.register_batch_interval_seconds == 10800
    assert settings.account_survival_enabled is True
    assert settings.dashboard_allowed_origins == ()
    assert settings.enable_proxy_pool is True
    assert settings.proxy_pool_size == 20
    assert settings.proxy_pool_regions == ("jp", "tw", "hk", "sg")
    assert settings.proxy_pool_config == (tmp_path / "clash_config.yaml").resolve()
    assert settings.cfmail_rotation_window == 10
    assert settings.cfmail_rotation_blacklist_threshold == 6
    assert settings.cfmail_rotation_max_successes == 2
    assert settings.account_survival_proxy == "http://127.0.0.1:7899"
    assert settings.responses_survival_state_file == (tmp_path / "state" / "responses_survival_tracker.json").resolve()
    assert settings.config_dir == (tmp_path / "config").resolve()
    assert settings.state_dir == (tmp_path / "state").resolve()
    assert settings.log_dir == (tmp_path / "logs").resolve()
    assert settings.pool_dir == (tmp_path / "pool").resolve()
    assert settings.validate_scope == "all"
    assert not hasattr(settings, "main_" + "pool_target")
    assert not hasattr(settings, "rotate_probe_max_count")
    assert not hasattr(settings, "promotion_probe_proxy")
    assert not hasattr(settings, "promotion_probe_timeout_seconds")


def test_app_settings_reads_dashboard_allowed_origins(monkeypatch) -> None:
    monkeypatch.setenv(
        "ZHUCE6_DASHBOARD_ALLOWED_ORIGINS",
        "http://127.0.0.1:8317, http://localhost:3000/ , https://dash.example.com",
    )

    settings = AppSettings.from_env()

    assert settings.dashboard_allowed_origins == (
        "http://127.0.0.1:8317",
        "http://localhost:3000",
        "https://dash.example.com",
    )



def test_app_settings_validate_cfmail_env(monkeypatch) -> None:
    for key in [
        "ZHUCE6_CFMAIL_API_TOKEN",
        "ZHUCE6_CFMAIL_CF_AUTH_EMAIL",
        "ZHUCE6_CFMAIL_CF_AUTH_KEY",
        "ZHUCE6_CFMAIL_CF_ACCOUNT_ID",
        "ZHUCE6_CFMAIL_CF_ZONE_ID",
        "ZHUCE6_CFMAIL_WORKER_NAME",
        "ZHUCE6_CFMAIL_ZONE_NAME",
    ]:
        monkeypatch.delenv(key, raising=False)

    settings = AppSettings.from_env()
    missing = settings.validate_cfmail_env()

    assert missing == [
        "ZHUCE6_CFMAIL_API_TOKEN",
        "ZHUCE6_CFMAIL_CF_ACCOUNT_ID",
        "ZHUCE6_CFMAIL_CF_ZONE_ID",
        "ZHUCE6_CFMAIL_WORKER_NAME",
        "ZHUCE6_CFMAIL_ZONE_NAME",
    ]


def test_app_settings_validate_cfmail_env_accepts_legacy_email_key_pair(monkeypatch) -> None:
    monkeypatch.delenv("ZHUCE6_CFMAIL_API_TOKEN", raising=False)
    monkeypatch.setenv("ZHUCE6_CFMAIL_CF_AUTH_EMAIL", "cf@example.com")
    monkeypatch.setenv("ZHUCE6_CFMAIL_CF_AUTH_KEY", "global-key")
    monkeypatch.setenv("ZHUCE6_CFMAIL_CF_ACCOUNT_ID", "account-1")
    monkeypatch.setenv("ZHUCE6_CFMAIL_CF_ZONE_ID", "zone-1")
    monkeypatch.setenv("ZHUCE6_CFMAIL_WORKER_NAME", "worker-one")
    monkeypatch.setenv("ZHUCE6_CFMAIL_ZONE_NAME", "example.com")

    settings = AppSettings.from_env()

    assert settings.validate_cfmail_env() == []



def test_app_settings_reads_sub2api_backend(monkeypatch) -> None:
    monkeypatch.setenv("ZHUCE6_BACKEND", "sub2api")
    monkeypatch.setenv("ZHUCE6_SUB2API_BASE_URL", "http://127.0.0.1:8080")
    monkeypatch.setenv("ZHUCE6_SUB2API_ADMIN_EMAIL", "admin@sub2api.local")
    monkeypatch.setenv("ZHUCE6_SUB2API_ADMIN_PASSWORD", "secret")
    monkeypatch.setenv("ZHUCE6_SUB2API_API_KEY", "api-key")

    settings = AppSettings.from_env()

    assert settings.backend == "sub2api"
    assert settings.sub2api_base_url == "http://127.0.0.1:8080"
    assert settings.sub2api_admin_email == "admin@sub2api.local"
    assert settings.sub2api_admin_password == "secret"
    assert settings.sub2api_api_key == "api-key"


def test_app_settings_no_longer_exposes_legacy_cpa_container_fields(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ZHUCE6_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("ZHUCE6_CPA_MANAGEMENT_MODE", "legacy")
    monkeypatch.setenv("ZHUCE6_CPA_REMOTE_MODE", "false")
    monkeypatch.setenv("ZHUCE6_CPA_CONTAINER", "legacy-container")
    monkeypatch.setenv("ZHUCE6_CPA_AUTH_DIR", "/legacy/auth")

    settings = AppSettings.from_env()

    assert not hasattr(settings, "cpa_container")
    assert not hasattr(settings, "cpa_auth_dir")
    assert not hasattr(settings, "cpa_management_mode")


def test_app_settings_ignores_legacy_cpa_management_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ZHUCE6_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("ZHUCE6_CPA_MANAGEMENT_MODE", "legacy")
    monkeypatch.setenv("ZHUCE6_CPA_REMOTE_MODE", "false")
    monkeypatch.setenv("ZHUCE6_CPA_CONTAINER", "legacy-container")
    monkeypatch.setenv("ZHUCE6_CPA_AUTH_DIR", "/legacy/auth")
    monkeypatch.setenv("ZHUCE6_CPA_MANAGEMENT_BASE_URL", "http://127.0.0.1:9000/v0/management")
    monkeypatch.setenv("ZHUCE6_CPA_MANAGEMENT_KEY", "key-1")

    settings = AppSettings.from_env()

    assert settings.cpa_management_base_url == "http://127.0.0.1:9000/v0/management"
    assert settings.cpa_management_key == "key-1"

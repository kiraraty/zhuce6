from __future__ import annotations

from dataclasses import dataclass

import core.setup_wizard as setup_wizard
from core.setup_wizard import run_setup_wizard


class _Elapsed:
    def __init__(self, seconds: float) -> None:
        self._seconds = seconds

    def total_seconds(self) -> float:
        return self._seconds


class _Response:
    def __init__(self, *, elapsed: float = 0.085, status_code: int = 200) -> None:
        self.elapsed = _Elapsed(elapsed)
        self.status_code = status_code


class _HttpxStub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append((url, kwargs))
        if "api.openai.com" in url:
            return _Response(elapsed=0.085, status_code=200)
        raise AssertionError(f"unexpected url: {url}")


@dataclass(frozen=True)
class _PreparedCfmail:
    api_token: str
    account_id: str
    zone_id: str
    worker_name: str
    worker_domain: str
    zone_name: str
    email_domain: str
    admin_password: str
    d1_name: str
    d1_database_id: str


def test_run_setup_wizard_writes_lite_cfmail_minimal_flow(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "ZHUCE6_HOST=0.0.0.0",
                "ZHUCE6_PORT=9000",
                "ZHUCE6_REGISTER_MAIL_PROVIDER=cfmail",
                f"ZHUCE6_CFMAIL_CONFIG_PATH={tmp_path / 'config' / 'cfmail_accounts.json'}",
                f"ZHUCE6_CFMAIL_ENV_FILE={tmp_path / 'config' / 'cfmail_provision.env'}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    httpx_stub = _HttpxStub()
    monkeypatch.setattr(setup_wizard, "httpx", httpx_stub)
    monkeypatch.setattr(
        setup_wizard,
        "_validate_cloudflare_credentials",
        lambda print_fn, **_kwargs: print_fn("  ✅ Cloudflare 凭据有效"),
    )

    def fake_prepare_runtime_cfmail_config(**kwargs):  # type: ignore[no-untyped-def]
        accounts_path = kwargs["accounts_path"]
        env_path = kwargs["provision_env_path"]
        accounts_path.parent.mkdir(parents=True, exist_ok=True)
        accounts_path.write_text('[{"name":"worker-one","worker_domain":"worker-one.demo.workers.dev","email_domain":"mail.example.com","admin_password":"super-secret","enabled":true}]\n', encoding="utf-8")
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(
            '\n'.join(
                [
                    'export ZHUCE6_CFMAIL_API_TOKEN="cf-token"',
                    'export ZHUCE6_CFMAIL_CF_ACCOUNT_ID="account-1"',
                    'export ZHUCE6_CFMAIL_CF_ZONE_ID="zone-1"',
                    'export ZHUCE6_CFMAIL_WORKER_NAME="worker-one"',
                    'export ZHUCE6_CFMAIL_ZONE_NAME="example.com"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return _PreparedCfmail(
            api_token="cf-token",
            account_id="account-1",
            zone_id="zone-1",
            worker_name="worker-one",
            worker_domain="worker-one.demo.workers.dev",
            zone_name="example.com",
            email_domain="mail.example.com",
            admin_password="super-secret",
            d1_name="zhuce6-cfmail-db",
            d1_database_id="db-1",
        )

    monkeypatch.setattr(setup_wizard.setup_cfmail, "prepare_runtime_cfmail_config", fake_prepare_runtime_cfmail_config)

    answers = iter(
        [
            "",  # mode -> lite
            "",  # host -> existing default
            "9100",
            "cfmail",
            "y",
            "1",
            "socks5://10.0.0.1:1080",
            "cf-token",
            "example.com",
            "worker-one",
            "mail.example.com",
            "super-secret",
            "y",
        ]
    )
    captured: list[str] = []

    result = run_setup_wizard(env_file=env_file, input_fn=lambda _prompt: next(answers), print_fn=captured.append)

    env_content = env_file.read_text(encoding="utf-8")
    assert "ZHUCE6_RUN_MODE=lite" in env_content
    assert "ZHUCE6_BACKEND=cpa" in env_content
    assert "ZHUCE6_PORT=9100" in env_content
    assert "ZHUCE6_PROXY_POOL_DIRECT_URLS=socks5://10.0.0.1:1080" in env_content
    assert "ZHUCE6_CFMAIL_API_TOKEN=cf-token" in env_content
    assert "ZHUCE6_CFMAIL_CF_ACCOUNT_ID=account-1" in env_content
    assert "ZHUCE6_CFMAIL_WORKER_NAME=worker-one" in env_content
    assert "ZHUCE6_D1_DATABASE_ID=db-1" in env_content
    assert "ZHUCE6_MAIN_POOL_TARGET" not in env_content
    assert result.cfmail_accounts_path == tmp_path / "config" / "cfmail_accounts.json"
    assert result.cfmail_env_path == tmp_path / "config" / "cfmail_provision.env"
    assert any("[1/5] 运行模式与后端" in line for line in captured)
    assert any("下一步:" in line for line in captured)
    assert any("doctor --fix" in line for line in captured)
    assert [url for url, _kwargs in httpx_stub.calls] == ["https://api.openai.com"]


def test_run_setup_wizard_full_cpa_writes_backend_config(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(setup_wizard, "httpx", _HttpxStub())

    answers = iter(
        [
            "full",
            "cpa",
            "127.0.0.1",
            "8000",
            "mailtm",
            "y",
            "1",
            "http://127.0.0.1:7899",
            "http://127.0.0.1:8317/v0/management",
            "mgmt-key",
            "y",
        ]
    )
    captured: list[str] = []

    run_setup_wizard(env_file=env_file, input_fn=lambda _prompt: next(answers), print_fn=captured.append)

    env_content = env_file.read_text(encoding="utf-8")
    assert "ZHUCE6_RUN_MODE=full" in env_content
    assert "ZHUCE6_BACKEND=cpa" in env_content
    assert "ZHUCE6_CPA_MANAGEMENT_BASE_URL=http://127.0.0.1:8317/v0/management" in env_content
    assert "ZHUCE6_CPA_MANAGEMENT_KEY=mgmt-key" in env_content
    assert "ZHUCE6_SUB2API_BASE_URL" not in env_content
    assert any("后端:" in line and "cpa" in line for line in captured)


def test_run_setup_wizard_full_sub2api_writes_backend_config(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(setup_wizard, "httpx", _HttpxStub())

    answers = iter(
        [
            "full",
            "sub2api",
            "127.0.0.1",
            "8000",
            "mailtm",
            "n",
            "http://127.0.0.1:7899",
            "http://127.0.0.1:8080",
            "password",
            "admin@sub2api.local",
            "secret",
            "y",
        ]
    )
    captured: list[str] = []

    run_setup_wizard(env_file=env_file, input_fn=lambda _prompt: next(answers), print_fn=captured.append)

    env_content = env_file.read_text(encoding="utf-8")
    assert "ZHUCE6_RUN_MODE=full" in env_content
    assert "ZHUCE6_BACKEND=sub2api" in env_content
    assert "ZHUCE6_SUB2API_BASE_URL=http://127.0.0.1:8080" in env_content
    assert "ZHUCE6_SUB2API_ADMIN_EMAIL=admin@sub2api.local" in env_content
    assert "ZHUCE6_SUB2API_ADMIN_PASSWORD=secret" in env_content
    assert "ZHUCE6_CPA_MANAGEMENT_BASE_URL" not in env_content
    assert any("后端:" in line and "sub2api" in line for line in captured)


def test_run_setup_wizard_reuses_existing_cfmail_config(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "cfmail_accounts.json").write_text(
        '[{"name":"existing-worker","worker_domain":"existing.workers.dev","email_domain":"mail.example.com","admin_password":"keep-me","enabled":true}]\n',
        encoding="utf-8",
    )
    (config_dir / "cfmail_provision.env").write_text(
        '\n'.join(
            [
                'export ZHUCE6_CFMAIL_API_TOKEN="old-token"',
                'export ZHUCE6_CFMAIL_CF_ACCOUNT_ID="account-old"',
                'export ZHUCE6_CFMAIL_CF_ZONE_ID="zone-old"',
                'export ZHUCE6_CFMAIL_WORKER_NAME="existing-worker"',
                'export ZHUCE6_CFMAIL_ZONE_NAME="example.com"',
                'export ZHUCE6_D1_DATABASE_ID="db-old"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    env_file.write_text(
        "\n".join(
            [
                "ZHUCE6_REGISTER_MAIL_PROVIDER=cfmail",
                f"ZHUCE6_CFMAIL_CONFIG_PATH={config_dir / 'cfmail_accounts.json'}",
                f"ZHUCE6_CFMAIL_ENV_FILE={config_dir / 'cfmail_provision.env'}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(setup_wizard, "httpx", _HttpxStub())
    monkeypatch.setattr(
        setup_wizard.setup_cfmail,
        "prepare_runtime_cfmail_config",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("should not regenerate cfmail config")),
    )

    answers = iter(
        [
            "lite",
            "127.0.0.1",
            "8000",
            "cfmail",
            "n",
            "http://127.0.0.1:7899",
            "y",
            "y",
        ]
    )
    captured: list[str] = []

    run_setup_wizard(env_file=env_file, input_fn=lambda _prompt: next(answers), print_fn=captured.append)

    env_content = env_file.read_text(encoding="utf-8")
    assert "ZHUCE6_CFMAIL_API_TOKEN=old-token" in env_content
    assert "ZHUCE6_CFMAIL_CF_ACCOUNT_ID=account-old" in env_content
    assert "ZHUCE6_D1_DATABASE_ID=db-old" in env_content
    assert any("复用现有 worker" in line for line in captured)


def test_run_setup_wizard_accepts_legacy_cfmail_global_key_for_fresh_init(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")

    httpx_stub = _HttpxStub()
    monkeypatch.setattr(setup_wizard, "httpx", httpx_stub)
    monkeypatch.setattr(
        setup_wizard,
        "_validate_cloudflare_credentials",
        lambda print_fn, **_kwargs: print_fn("  ✅ Cloudflare 凭据有效"),
    )
    captured_prepare: dict[str, object] = {}

    def fake_prepare_runtime_cfmail_config(**kwargs):  # type: ignore[no-untyped-def]
        captured_prepare.update(kwargs)
        accounts_path = kwargs["accounts_path"]
        env_path = kwargs["provision_env_path"]
        accounts_path.parent.mkdir(parents=True, exist_ok=True)
        accounts_path.write_text(
            '[{"name":"worker-one","worker_domain":"worker-one.demo.workers.dev","email_domain":"mail.example.com","admin_password":"super-secret","enabled":true}]\n',
            encoding="utf-8",
        )
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(
            '\n'.join(
                [
                    'export ZHUCE6_CFMAIL_API_TOKEN=""',
                    'export ZHUCE6_CFMAIL_CF_AUTH_EMAIL="cf@example.com"',
                    'export ZHUCE6_CFMAIL_CF_AUTH_KEY="global-key"',
                    'export ZHUCE6_CFMAIL_CF_ACCOUNT_ID="account-1"',
                    'export ZHUCE6_CFMAIL_CF_ZONE_ID="zone-1"',
                    'export ZHUCE6_CFMAIL_WORKER_NAME="worker-one"',
                    'export ZHUCE6_CFMAIL_ZONE_NAME="example.com"',
                    'export ZHUCE6_D1_DATABASE_ID="db-1"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return _PreparedCfmail(
            api_token="",
            account_id="account-1",
            zone_id="zone-1",
            worker_name="worker-one",
            worker_domain="worker-one.demo.workers.dev",
            zone_name="example.com",
            email_domain="mail.example.com",
            admin_password="super-secret",
            d1_name="zhuce6-cfmail-db",
            d1_database_id="db-1",
        )

    monkeypatch.setattr(setup_wizard.setup_cfmail, "prepare_runtime_cfmail_config", fake_prepare_runtime_cfmail_config)

    answers = iter(
        [
            "lite",
            "127.0.0.1",
            "9100",
            "cfmail",
            "n",
            "http://127.0.0.1:7899",
            "",  # token left blank
            "cf@example.com",
            "global-key",
            "email-api.example.com",
            "example.com",
            "worker-one",
            "mail.example.com",
            "super-secret",
            "y",
        ]
    )
    captured: list[str] = []

    run_setup_wizard(env_file=env_file, input_fn=lambda _prompt: next(answers), print_fn=captured.append)

    env_content = env_file.read_text(encoding="utf-8")
    assert "ZHUCE6_CFMAIL_API_TOKEN=" in env_content
    assert "ZHUCE6_CFMAIL_CF_AUTH_EMAIL=cf@example.com" in env_content
    assert "ZHUCE6_CFMAIL_CF_AUTH_KEY=global-key" in env_content
    assert "ZHUCE6_D1_DATABASE_ID=db-1" in env_content
    assert captured_prepare["api_token"] == ""
    assert captured_prepare["auth_email"] == "cf@example.com"
    assert captured_prepare["auth_key"] == "global-key"
    assert captured_prepare["worker_domain"] == "email-api.example.com"
    assert any("Cloudflare 全局 Key" in line for line in captured)


def test_run_setup_wizard_clash_mode_prints_sslocal_guidance_when_missing(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")

    monkeypatch.setattr(setup_wizard, "httpx", _HttpxStub())
    monkeypatch.setattr(setup_wizard.shutil, "which", lambda _name: None)

    answers = iter(
        [
            "full",
            "cpa",
            "127.0.0.1",
            "8000",
            "mailtm",
            "y",
            "2",
            str(tmp_path / "clash.yaml"),
            "http://127.0.0.1:8317/v0/management",
            "",
            "y",
        ]
    )
    captured: list[str] = []

    run_setup_wizard(env_file=env_file, input_fn=lambda _prompt: next(answers), print_fn=captured.append)

    env_content = env_file.read_text(encoding="utf-8")
    assert f"ZHUCE6_PROXY_POOL_CONFIG={tmp_path / 'clash.yaml'}" in env_content
    assert any("shadowsocks-rust" in line for line in captured)


def test_run_setup_wizard_decline_save_does_not_write_env_or_cfmail_files(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("ORIGINAL=1\n", encoding="utf-8")

    monkeypatch.setattr(setup_wizard, "httpx", _HttpxStub())
    monkeypatch.setattr(
        setup_wizard,
        "_validate_cloudflare_credentials",
        lambda print_fn, **_kwargs: print_fn("  ✅ Cloudflare 凭据有效"),
    )
    monkeypatch.setattr(
        setup_wizard.setup_cfmail,
        "prepare_runtime_cfmail_config",
        lambda **_kwargs: _PreparedCfmail(
            api_token="cf-token",
            account_id="account-1",
            zone_id="zone-1",
            worker_name="worker-one",
            worker_domain="worker-one.demo.workers.dev",
            zone_name="example.com",
            email_domain="mail.example.com",
            admin_password="secret",
            d1_name="db",
            d1_database_id="db-1",
        ),
    )

    answers = iter(
        [
            "lite",
            "127.0.0.1",
            "8000",
            "cfmail",
            "n",
            "http://127.0.0.1:7899",
            "cf-token",
            "example.com",
            "worker-one",
            "mail.example.com",
            "secret",
            "n",
        ]
    )
    captured: list[str] = []

    result = run_setup_wizard(env_file=env_file, input_fn=lambda _prompt: next(answers), print_fn=captured.append)

    assert env_file.read_text(encoding="utf-8") == "ORIGINAL=1\n"
    assert not (tmp_path / "config" / "cfmail_accounts.json").exists()
    assert not (tmp_path / "config" / "cfmail_provision.env").exists()
    assert result.cfmail_accounts_path is None
    assert result.cfmail_env_path is None
    assert any("已取消保存" in line for line in captured)

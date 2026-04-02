from __future__ import annotations

import json
import sys
import tomllib

from core.cfmail import build_cfmail_accounts, load_cfmail_accounts_from_file
from scripts import setup_cfmail


def test_write_cfmail_accounts_json_is_runtime_compatible(tmp_path):
    output_path = tmp_path / "cfmail_accounts.json"

    setup_cfmail.write_cfmail_accounts_json(
        output_path,
        worker_domain="zhuce6-cfmail.demo-subdomain.workers.dev",
        email_domain="example.com",
        worker_name="zhuce6-cfmail",
        admin_password="secret-admin-password",
    )

    raw_accounts = load_cfmail_accounts_from_file(output_path)
    normalized = build_cfmail_accounts(raw_accounts)

    assert json.loads(output_path.read_text(encoding="utf-8")) == [
        {
            "name": "zhuce6-cfmail",
            "worker_domain": "zhuce6-cfmail.demo-subdomain.workers.dev",
            "email_domain": "example.com",
            "admin_password": "secret-admin-password",
            "enabled": True,
        }
    ]
    assert len(normalized) == 1
    assert normalized[0].name == "zhuce6-cfmail"
    assert normalized[0].worker_domain == "zhuce6-cfmail.demo-subdomain.workers.dev"
    assert normalized[0].email_domain == "example.com"
    assert normalized[0].admin_password == "secret-admin-password"


def test_write_cfmail_provision_env_contains_required_exports(tmp_path):
    output_path = tmp_path / "cfmail_provision.env"

    setup_cfmail.write_cfmail_provision_env(
        output_path,
        api_token="cf-token",
        account_id="account-123",
        zone_id="zone-456",
        worker_name="zhuce6-cfmail",
        zone_name="example.com",
        d1_database_id="db-789",
    )

    content = output_path.read_text(encoding="utf-8")
    assert 'export ZHUCE6_CFMAIL_API_TOKEN="cf-token"' in content
    assert 'export ZHUCE6_CFMAIL_CF_AUTH_EMAIL=""' in content
    assert 'export ZHUCE6_CFMAIL_CF_AUTH_KEY=""' in content
    assert 'export ZHUCE6_CFMAIL_CF_ACCOUNT_ID="account-123"' in content
    assert 'export ZHUCE6_CFMAIL_CF_ZONE_ID="zone-456"' in content
    assert 'export ZHUCE6_CFMAIL_WORKER_NAME="zhuce6-cfmail"' in content
    assert 'export ZHUCE6_CFMAIL_ZONE_NAME="example.com"' in content
    assert 'export ZHUCE6_D1_DATABASE_ID="db-789"' in content


def test_write_worker_wrangler_emits_runtime_required_vars(tmp_path):
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir()

    wrangler_path = setup_cfmail.write_worker_wrangler(
        worker_dir=worker_dir,
        worker_name="zhuce6-cfmail",
        account_id="account-123",
        database_id="db-456",
        database_name="zhuce6-cfmail-db",
        email_domain="example.com",
        admin_password="secret-admin-password",
        jwt_secret="secret-jwt",
        compatibility_date="2025-04-01",
    )

    document = tomllib.loads(wrangler_path.read_text(encoding="utf-8"))
    assert document["name"] == "zhuce6-cfmail"
    assert document["account_id"] == "account-123"
    assert document["main"] == "src/worker.ts"
    assert document["compatibility_date"] == "2025-04-01"
    assert document["vars"]["DOMAINS"] == ["example.com"]
    assert document["vars"]["DEFAULT_DOMAINS"] == ["example.com"]
    assert document["vars"]["ADMIN_PASSWORDS"] == ["secret-admin-password"]
    assert document["vars"]["JWT_SECRET"] == "secret-jwt"
    assert document["d1_databases"][0]["binding"] == "DB"
    assert document["d1_databases"][0]["database_id"] == "db-456"
    assert document["d1_databases"][0]["database_name"] == "zhuce6-cfmail-db"


def test_build_parser_exposes_expected_defaults():
    parser = setup_cfmail.build_parser()

    args = parser.parse_args(["--api-token", "cfpat_xxx", "--zone-name", "example.com"])

    assert args.worker_name == "zhuce6-cfmail"
    assert args.d1_name == "zhuce6-cfmail-db"
    assert args.mail_domain is None
    assert args.skip_clone is False


def test_build_parser_supports_legacy_cloudflare_global_key_args():
    parser = setup_cfmail.build_parser()

    args = parser.parse_args(
        [
            "--auth-email",
            "cf@example.com",
            "--auth-key",
            "global-key",
            "--zone-name",
            "example.com",
        ]
    )

    assert args.api_token == ""
    assert args.auth_email == "cf@example.com"
    assert args.auth_key == "global-key"
    assert args.zone_name == "example.com"


def test_cloudflare_client_supports_legacy_global_key_auth(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)

        def close(self) -> None:
            return

    class FakeHttpxModule:
        Client = FakeClient
        HTTPError = RuntimeError

    monkeypatch.setitem(sys.modules, "httpx", FakeHttpxModule())

    client = setup_cfmail.CloudflareClient("", auth_email="cf@example.com", auth_key="global-key")
    client.close()

    headers = captured["headers"]
    assert headers["X-Auth-Email"] == "cf@example.com"
    assert headers["X-Auth-Key"] == "global-key"
    assert "Authorization" not in headers


def test_cloudflare_client_verify_token_uses_user_endpoint_for_legacy_global_key(monkeypatch) -> None:
    requested: list[tuple[str, str]] = []

    class FakeResponse:
        status_code = 200
        is_success = True
        text = ""

        @staticmethod
        def json() -> dict[str, object]:
            return {
                "success": True,
                "result": {
                    "id": "user-1",
                    "email": "cf@example.com",
                },
            }

    class FakeClient:
        def __init__(self, **_kwargs):  # type: ignore[no-untyped-def]
            return

        def request(self, method, path, params=None, json=None):  # type: ignore[no-untyped-def]
            requested.append((method, path))
            return FakeResponse()

        def close(self) -> None:
            return

    class FakeHttpxModule:
        Client = FakeClient
        HTTPError = RuntimeError

    monkeypatch.setitem(sys.modules, "httpx", FakeHttpxModule())

    with setup_cfmail.CloudflareClient("", auth_email="cf@example.com", auth_key="global-key") as client:
        result = client.verify_token()

    assert requested == [("GET", "/user")]
    assert result["status"] == "active"
    assert result["email"] == "cf@example.com"


def test_prepare_runtime_cfmail_config_accepts_existing_worker_domain_override(monkeypatch, tmp_path) -> None:
    requested: list[tuple[str, str]] = []

    class FakeCloudflareClient:
        def __init__(self, api_token, *, auth_email="", auth_key="", timeout=30.0):  # type: ignore[no-untyped-def]
            assert api_token == ""
            assert auth_email == "cf@example.com"
            assert auth_key == "global-key"

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
            return

        def verify_token(self):  # type: ignore[no-untyped-def]
            requested.append(("GET", "/user"))
            return {"status": "active"}

        def resolve_zone(self, zone_name):  # type: ignore[no-untyped-def]
            assert zone_name == "example.com"
            return {"id": "zone-1", "account": {"id": "account-1"}}

        def ensure_d1_database(self, account_id, database_name):  # type: ignore[no-untyped-def]
            assert account_id == "account-1"
            assert database_name == "zhuce6-cfmail-db"
            return {"uuid": "db-1"}

        def get_workers_subdomain(self, account_id):  # type: ignore[no-untyped-def]
            raise AssertionError("worker_domain override path must not call get_workers_subdomain")

    class FakeProvisioner:
        def __init__(self, *, config_path, settings, proxy_url=None):  # type: ignore[no-untyped-def]
            self.smoke_calls = []

        def smoke_test(self, worker_domain, admin_password, email_domain):  # type: ignore[no-untyped-def]
            self.smoke_calls.append((worker_domain, admin_password, email_domain))
            return

        def rotate_active_domain(self):  # type: ignore[no-untyped-def]
            raise AssertionError("valid override path must not rotate")

    monkeypatch.setattr(setup_cfmail, "CloudflareClient", FakeCloudflareClient)
    monkeypatch.setattr(setup_cfmail, "CfmailProvisioner", FakeProvisioner)

    accounts_path = tmp_path / "config" / "cfmail_accounts.json"
    provision_env_path = tmp_path / "config" / "cfmail_provision.env"
    result = setup_cfmail.prepare_runtime_cfmail_config(
        api_token="",
        auth_email="cf@example.com",
        auth_key="global-key",
        worker_domain="email-api.example.com",
        zone_name="example.com",
        worker_name="worker-one",
        mail_domain="mail.example.com",
        admin_password="super-secret",
        accounts_path=accounts_path,
        provision_env_path=provision_env_path,
    )

    assert requested == [("GET", "/user")]
    assert result.worker_domain == "email-api.example.com"
    assert '"worker_domain": "email-api.example.com"' in accounts_path.read_text(encoding="utf-8")


def test_prepare_runtime_cfmail_config_rotates_when_existing_domain_is_invalid(monkeypatch, tmp_path) -> None:
    class FakeCloudflareClient:
        def __init__(self, api_token, *, auth_email="", auth_key="", timeout=30.0):  # type: ignore[no-untyped-def]
            assert api_token == ""
            assert auth_email == "cf@example.com"
            assert auth_key == "global-key"

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
            return

        def verify_token(self):  # type: ignore[no-untyped-def]
            return {"status": "active"}

        def resolve_zone(self, zone_name):  # type: ignore[no-untyped-def]
            return {"id": "zone-1", "account": {"id": "account-1"}}

        def ensure_d1_database(self, account_id, database_name):  # type: ignore[no-untyped-def]
            return {"uuid": "db-1"}

        def get_workers_subdomain(self, account_id):  # type: ignore[no-untyped-def]
            raise AssertionError("worker_domain override path must not call get_workers_subdomain")

    class FakeProvisionResult:
        def __init__(self, success: bool, new_domain: str, error: str = "") -> None:
            self.success = success
            self.new_domain = new_domain
            self.error = error

    class FakeProvisioner:
        last_instance = None

        def __init__(self, *, config_path, settings, proxy_url=None):  # type: ignore[no-untyped-def]
            self.config_path = config_path
            self.settings = settings
            self.proxy_url = proxy_url
            self.smoke_calls = []
            self.rotate_calls = 0
            FakeProvisioner.last_instance = self

        def smoke_test(self, worker_domain, admin_password, email_domain):  # type: ignore[no-untyped-def]
            self.smoke_calls.append((worker_domain, admin_password, email_domain))
            raise RuntimeError("HTTP 400 创建邮箱地址失败: 无效的域名")

        def rotate_active_domain(self):  # type: ignore[no-untyped-def]
            self.rotate_calls += 1
            self.config_path.write_text(
                '[{\"name\":\"worker-one\",\"worker_domain\":\"email-api.example.com\",\"email_domain\":\"auto-fresh.example.com\",\"admin_password\":\"super-secret\",\"enabled\":true}]\\n',
                encoding="utf-8",
            )
            return FakeProvisionResult(True, "auto-fresh.example.com")

    monkeypatch.setattr(setup_cfmail, "CloudflareClient", FakeCloudflareClient)
    monkeypatch.setattr(setup_cfmail, "CfmailProvisioner", FakeProvisioner)

    accounts_path = tmp_path / "config" / "cfmail_accounts.json"
    provision_env_path = tmp_path / "config" / "cfmail_provision.env"
    result = setup_cfmail.prepare_runtime_cfmail_config(
        api_token="",
        auth_email="cf@example.com",
        auth_key="global-key",
        worker_domain="email-api.example.com",
        zone_name="example.com",
        worker_name="worker-one",
        mail_domain="stale.example.com",
        admin_password="super-secret",
        accounts_path=accounts_path,
        provision_env_path=provision_env_path,
    )

    fake = FakeProvisioner.last_instance
    assert fake is not None
    assert fake.smoke_calls == [("email-api.example.com", "super-secret", "stale.example.com")]
    assert fake.rotate_calls == 1
    assert result.worker_domain == "email-api.example.com"
    assert result.email_domain == "auto-fresh.example.com"
    assert '"email_domain":"auto-fresh.example.com"' in accounts_path.read_text(encoding="utf-8")

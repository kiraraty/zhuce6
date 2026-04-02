import json
from pathlib import Path

from core.base_platform import RegisterConfig
from platforms.chatgpt.plugin import ChatGPTPlatform
from platforms.chatgpt.register import RegistrationResult


class _FakeMailbox:
    def get_email(self):  # type: ignore[no-untyped-def]
        raise AssertionError("mailbox should not be used in this test")

    def wait_for_code(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("mailbox should not be used in this test")


def test_run_register_once_persists_password_in_pool_file(monkeypatch, tmp_path: Path) -> None:
    class FakeEngine:
        def __init__(self, email_service, proxy_url=None, **kwargs):  # type: ignore[no-untyped-def]
            del kwargs
            self.email_service = email_service
            self.proxy_url = proxy_url
            self.email = None
            self.password = None

        def run(self):  # type: ignore[no-untyped-def]
            return RegistrationResult(
                success=True,
                stage="completed",
                email="demo@example.com",
                password="pw-secret",
                account_id="acct-123",
                access_token="access-123",
                refresh_token="refresh-123",
                id_token="id-123",
                metadata={"expired": "2026-03-30T00:00:00Z"},
            )

    monkeypatch.setattr("platforms.chatgpt.register.RegistrationEngine", FakeEngine)

    platform = ChatGPTPlatform(
        config=RegisterConfig(proxy="http://127.0.0.1:7899", extra={"mail_provider": "cfmail"}),
        mailbox=_FakeMailbox(),
    )

    payload = platform.run_register_once(write_pool=True, pool_dir=tmp_path)

    pool_file = Path(payload["pool_file"])
    data = json.loads(pool_file.read_text(encoding="utf-8"))

    assert payload["success"] is True
    assert payload["written_to_pool"] is True
    assert pool_file.exists()
    assert data["email"] == "demo@example.com"
    assert data["password"] == "pw-secret"
    assert data["mail_provider"] == "cfmail"
    assert data["mailbox"]["email"] == "demo@example.com"
    assert data["mailbox"]["account_id"] == ""
    assert data["mailbox"]["extra"] == {}
    assert data["access_token"] == "access-123"
    assert data["refresh_token"] == "refresh-123"


def test_run_register_once_persists_mailbox_account_context(monkeypatch, tmp_path: Path) -> None:
    class FakeMailbox:
        pass

    mailbox = FakeMailbox()

    class FakeEngine:
        def __init__(self, email_service, proxy_url=None, **kwargs):  # type: ignore[no-untyped-def]
            del kwargs
            self.email_service = email_service
            self.proxy_url = proxy_url
            self.email = None
            self.password = None

        def run(self):  # type: ignore[no-untyped-def]
            self.email_service._account = type(  # type: ignore[attr-defined]
                "Account",
                (),
                {
                    "email": "mailbox@example.com",
                    "account_id": "jwt-123",
                    "extra": {"api_base": "https://email-api.example.test", "config_name": "cfmail-a"},
                },
            )()
            return RegistrationResult(
                success=True,
                stage="completed",
                email="mailbox@example.com",
                password="pw-mailbox",
                account_id="acct-mailbox",
                access_token="access-mailbox",
                refresh_token="refresh-mailbox",
                id_token="id-mailbox",
                metadata={"expired": "2026-03-30T00:00:00Z"},
            )

    monkeypatch.setattr("platforms.chatgpt.register.RegistrationEngine", FakeEngine)

    platform = ChatGPTPlatform(
        config=RegisterConfig(proxy="http://127.0.0.1:7899", extra={"mail_provider": "cfmail"}),
        mailbox=mailbox,
    )

    payload = platform.run_register_once(write_pool=True, pool_dir=tmp_path)
    data = json.loads(Path(payload["pool_file"]).read_text(encoding="utf-8"))

    assert data["mailbox"]["email"] == "mailbox@example.com"
    assert data["mailbox"]["account_id"] == "jwt-123"
    assert data["mailbox"]["extra"] == {
        "api_base": "https://email-api.example.test",
        "config_name": "cfmail-a",
    }

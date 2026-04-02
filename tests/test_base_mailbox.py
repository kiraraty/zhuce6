import json

import pytest

from core.base_mailbox import BaseMailbox, MailboxAccount, create_mailbox
from core.cfmail import DEFAULT_CFMAIL_MANAGER, CfMailMailbox, CfmailAccount


class FakeResponse:
    def __init__(self, payload, status_code: int = 200):  # type: ignore[no-untyped-def]
        self._payload = payload
        self.status_code = status_code
        self.content = b"payload"
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):  # type: ignore[no-untyped-def]
        return self._payload


class DummyCfmailManager:
    def __init__(self) -> None:
        self.account = CfmailAccount(
            name="demo",
            worker_domain="email-api.example.com",
            email_domain="mail.example.com",
            admin_password="secret",
        )
        self.successes = 0
        self.failures: list[str] = []

    def reload_if_needed(self) -> bool:
        return False

    def select_account(self, profile_name=None):  # type: ignore[no-untyped-def]
        del profile_name
        return self.account

    def record_success(self, account_name: str) -> None:
        assert account_name == self.account.name
        self.successes += 1

    def record_failure(self, account_name: str, reason: str = "") -> None:
        assert account_name == self.account.name
        self.failures.append(reason)

    def account_names(self) -> str:
        return self.account.name


class PartialMailbox(BaseMailbox):
    def get_email(self) -> MailboxAccount:
        return MailboxAccount(email="demo@example.com")


class DummyMailbox(BaseMailbox):
    def get_email(self) -> MailboxAccount:
        return MailboxAccount(email="demo@example.com", account_id="token")

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set[str] | None = None,
    ) -> str:
        del account, keyword, timeout, before_ids
        return "123456"

    def get_current_ids(self, account: MailboxAccount) -> set[str]:
        del account
        return {"msg-1"}


def test_base_mailbox_requires_all_abstract_methods() -> None:
    with pytest.raises(TypeError):
        PartialMailbox()


def test_mailbox_account_and_base_interface_contract() -> None:
    mailbox = DummyMailbox()
    account = mailbox.get_email()

    assert account == MailboxAccount(email="demo@example.com", account_id="token", extra={})
    assert mailbox.get_current_ids(account) == {"msg-1"}
    assert mailbox.wait_for_code(account) == "123456"


def test_create_mailbox_only_supports_cfmail() -> None:
    mailbox = create_mailbox("cfmail")

    assert isinstance(mailbox, CfMailMailbox)
    assert mailbox.manager is DEFAULT_CFMAIL_MANAGER

    with pytest.raises(ValueError, match="Unsupported mailbox provider"):
        create_mailbox("mailtm")


def test_cfmail_get_email_retries_transient_transport_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = DummyCfmailManager()
    mailbox = CfMailMailbox(manager=manager, proxy="socks5://127.0.0.1:18043")
    calls = {"count": 0}
    seen_proxies: list[object] = []

    def fake_post(url, **kwargs):  # type: ignore[no-untyped-def]
        del url
        calls["count"] += 1
        seen_proxies.append(kwargs.get("proxies"))
        if calls["count"] < 3:
            raise RuntimeError(
                "Failed to perform, curl: (35) TLS connect error: "
                "error:00000000:OPENSSL_internal:invalid library"
            )
        return FakeResponse(
            {
                "address": "ocdemo@mail.example.com",
                "jwt": "jwt-demo",
            }
        )

    monkeypatch.setattr("core.cfmail.cffi_requests.post", fake_post)
    monkeypatch.setattr("core.cfmail.time.sleep", lambda *_args, **_kwargs: None)

    account = mailbox.get_email()

    assert calls["count"] == 3
    assert seen_proxies == [None, None, None]
    assert account.email == "ocdemo@mail.example.com"
    assert account.account_id == "jwt-demo"
    assert manager.successes == 1
    assert manager.failures == []


def test_cfmail_get_email_raises_after_exhausting_transient_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = DummyCfmailManager()
    mailbox = CfMailMailbox(manager=manager, proxy="socks5://127.0.0.1:18043")
    calls = {"count": 0}

    def fake_post(url, **kwargs):  # type: ignore[no-untyped-def]
        del url, kwargs
        calls["count"] += 1
        raise RuntimeError("Failed to perform, curl: (28) Connection timed out after 15000 milliseconds.")

    monkeypatch.setattr("core.cfmail.cffi_requests.post", fake_post)
    monkeypatch.setattr("core.cfmail.time.sleep", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="curl: \\(28\\)"):
        mailbox.get_email()

    assert calls["count"] == 3
    assert len(manager.failures) == 1
    assert "new_address exception" in manager.failures[0]


def test_cfmail_get_email_retries_retryable_http_statuses(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = DummyCfmailManager()
    mailbox = CfMailMailbox(manager=manager)
    calls = {"count": 0}

    def fake_post(url, **kwargs):  # type: ignore[no-untyped-def]
        del url, kwargs
        calls["count"] += 1
        if calls["count"] < 3:
            return FakeResponse({"error": "temporary upstream failure"}, status_code=503)
        return FakeResponse({"address": "ocrun@mail.example.com", "jwt": "jwt-run"})

    monkeypatch.setattr("core.cfmail.cffi_requests.post", fake_post)
    monkeypatch.setattr("core.cfmail.time.sleep", lambda *_args, **_kwargs: None)

    account = mailbox.get_email()

    assert calls["count"] == 3
    assert account.email == "ocrun@mail.example.com"
    assert account.account_id == "jwt-run"
    assert manager.successes == 1


def test_cfmail_get_email_includes_http_400_body_snippet(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = DummyCfmailManager()
    mailbox = CfMailMailbox(manager=manager)

    def fake_post(url, **kwargs):  # type: ignore[no-untyped-def]
        del url, kwargs
        return FakeResponse({"error": "D1 database full"}, status_code=400)

    monkeypatch.setattr("core.cfmail.cffi_requests.post", fake_post)

    with pytest.raises(RuntimeError, match=r"HTTP 400.*D1 database full"):
        mailbox.get_email()

    assert len(manager.failures) == 1
    assert "HTTP 400" in manager.failures[0]


def test_cfmail_wait_for_code_uses_expanded_window_and_records_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = DummyCfmailManager()
    mailbox = CfMailMailbox(manager=manager, proxy="socks5://127.0.0.1:18043")
    monkeypatch.setattr("core.cfmail.CFMAIL_WAIT_PROGRESS_CALLBACK", None)
    account = MailboxAccount(
        email="ocdemo@mail.example.com",
        account_id="jwt-demo",
        extra={"api_base": "https://email-api.example.com", "config_name": "demo"},
    )
    captured_limits: list[int] = []

    class WaitResponse:
        def __init__(self, payload):
            self.status_code = 200
            self.content = b"{}"
            self._payload = payload

        def json(self):
            return self._payload

    responses = [
        WaitResponse({"results": [{"id": "old-1", "address": account.email, "raw": "stale"}]}),
        WaitResponse({"results": [{"id": "new-1", "address": account.email, "raw": "Your ChatGPT code is 654321"}]}),
    ]

    def fake_request_with_retry(**kwargs):  # type: ignore[no-untyped-def]
        captured_limits.append(int(kwargs["params"]["limit"]))
        return responses.pop(0)

    tick = iter([100.0, 100.0, 100.2, 100.5, 101.0, 101.0, 101.2, 101.3, 101.4])
    monkeypatch.setattr(mailbox, "_request_with_retry", fake_request_with_retry)
    monkeypatch.setattr("core.cfmail.CFMAIL_WAIT_ABORT_PREDICATE", None)
    monkeypatch.setattr("core.cfmail.time.sleep", lambda *_a, **_k: None)
    monkeypatch.setattr("core.cfmail.time.time", lambda: next(tick))

    current_ids = mailbox.get_current_ids(account)
    code = mailbox.wait_for_code(account, timeout=30, before_ids=current_ids)

    assert current_ids == {"old-1"}
    assert code == "654321"
    assert captured_limits == [30, 30]
    assert mailbox.last_wait_diagnostics["first_message_seen_at"] == 100.5
    assert mailbox.last_wait_diagnostics["matched_message_at"] == 101.0
    assert mailbox.last_wait_diagnostics["poll_count"] == 1


def test_cfmail_mailbox_uses_direct_egress_even_when_register_proxy_is_configured() -> None:
    mailbox = CfMailMailbox(manager=DummyCfmailManager(), proxy="socks5://127.0.0.1:18043")

    assert mailbox.proxies is None


def test_cfmail_wait_for_code_aborts_when_rotation_predicate_requests_it(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = DummyCfmailManager()
    mailbox = CfMailMailbox(manager=manager)
    monkeypatch.setattr("core.cfmail.CFMAIL_WAIT_PROGRESS_CALLBACK", None)
    account = MailboxAccount(
        email="ocdemo@mail.example.com",
        account_id="jwt-demo",
        extra={"api_base": "https://email-api.example.com", "config_name": "demo"},
    )

    monkeypatch.setattr("core.cfmail.CFMAIL_WAIT_ABORT_PREDICATE", lambda _account: True)

    code = mailbox.wait_for_code(account, timeout=30, before_ids=set())

    assert code == ""
    assert mailbox.last_wait_diagnostics["aborted"] is True
    assert mailbox.last_wait_diagnostics["abort_reason"] == "rotation_or_stoploss"


def test_cfmail_wait_for_code_ignores_messages_older_than_not_before_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = DummyCfmailManager()
    mailbox = CfMailMailbox(manager=manager)
    monkeypatch.setattr("core.cfmail.CFMAIL_WAIT_PROGRESS_CALLBACK", None)
    account = MailboxAccount(
        email="ocdemo@mail.example.com",
        account_id="jwt-demo",
        extra={"api_base": "https://email-api.example.com", "config_name": "demo"},
    )

    class WaitResponse:
        def __init__(self, payload):
            self.status_code = 200
            self.content = b"{}"
            self._payload = payload

        def json(self):
            return self._payload

    responses = [
        WaitResponse(
            {
                "results": [
                    {
                        "id": "old-msg",
                        "address": account.email,
                        "raw": "Your ChatGPT code is 111111",
                        "createdAt": "2026-03-29T05:00:00Z",
                    },
                    {
                        "id": "new-msg",
                        "address": account.email,
                        "raw": "Your ChatGPT code is 222222",
                        "createdAt": "2026-03-29T05:00:30Z",
                    },
                ]
            }
        )
    ]

    monkeypatch.setattr(mailbox, "_request_with_retry", lambda **kwargs: responses.pop(0))
    monkeypatch.setattr("core.cfmail.CFMAIL_WAIT_ABORT_PREDICATE", None)
    monkeypatch.setattr("core.cfmail.time.sleep", lambda *_a, **_k: None)
    monkeypatch.setattr("core.cfmail.time.time", lambda: 1743224431.0)

    code = mailbox.wait_for_code(
        account,
        timeout=30,
        before_ids=set(),
        not_before_timestamp=1774760430.0,
    )

    assert code == "222222"

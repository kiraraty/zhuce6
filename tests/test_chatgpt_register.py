import json
from dataclasses import dataclass

from core.http_client import RequestConfig
import platforms.chatgpt.register as register_module
from platforms.chatgpt.http_client import OpenAIHTTPClient
from platforms.chatgpt.oauth import OAuthStart
from platforms.chatgpt.register import RegistrationEngine, SignupFormResult
from platforms.chatgpt.token_refresh import TokenRefreshResult


class DummyEmailService:
    def create_email(self, config=None):  # type: ignore[no-untyped-def]
        del config
        return {"email": "unused@example.com"}

    def get_verification_code(self, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        return ""


@dataclass
class FakeCookieItem:
    name: str
    value: str


class FakeCookies(dict[str, str]):
    @property
    def jar(self) -> list[FakeCookieItem]:
        return [FakeCookieItem(name=name, value=value) for name, value in self.items()]


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        *,
        url: str = "",
        headers: dict[str, str] | None = None,
        json_data: dict[str, object] | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self.url = url
        self.headers = headers or {}
        self._json_data = json_data
        self.text = text or (json.dumps(json_data) if json_data is not None else "")

    def json(self) -> dict[str, object]:
        if self._json_data is None:
            raise ValueError("json unavailable")
        return self._json_data


def _workspace_cookie(workspace_id: str) -> str:
    payload = json.dumps({"workspaces": [{"id": workspace_id}]}, separators=(",", ":")).encode("utf-8")
    encoded = register_module.base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{encoded}.sig"


class FakeSession:
    def __init__(self) -> None:
        self.cookies = FakeCookies({"oai-client-auth-session": _workspace_cookie("ws-123")})
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def get(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(("GET", url, kwargs))
        if url.startswith("https://auth.openai.com/oauth/authorize"):
            self.cookies["oai-did"] = "did-123"
            self.cookies["login_session"] = "login-session"
            return FakeResponse(200, url="https://auth.openai.com/log-in")
        if url == "https://auth.openai.com/continue-after-password":
            return FakeResponse(200, url=url)
        raise AssertionError(f"unexpected GET: {url}")

    def post(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(("POST", url, kwargs))
        if url.endswith("/authorize/continue"):
            return FakeResponse(
                200,
                url=url,
                json_data={"continue_url": "/log-in/password", "page": {"type": "password"}},
            )
        if url.endswith("/password/verify"):
            return FakeResponse(
                200,
                url=url,
                json_data={"continue_url": "/continue-after-password", "page": {"type": "consent"}},
            )
        if url.endswith("/workspace/select"):
            return FakeResponse(
                200,
                url=url,
                json_data={
                    "continue_url": "/organization-continue",
                    "data": {"orgs": [{"id": "org-456", "projects": [{"id": "proj-789"}]}]},
                },
            )
        if url.endswith("/organization/select"):
            return FakeResponse(
                302,
                url=url,
                headers={
                    "Location": "http://localhost:1455/auth/callback?code=oauth-code&state=demo-state",
                },
            )
        raise AssertionError(f"unexpected POST: {url}")


class NoWorkspaceSession:
    def __init__(self) -> None:
        self.cookies = FakeCookies({})
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def get(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(("GET", url, kwargs))
        if url.endswith("/authorize/continue"):
            return FakeResponse(200, url=url)
        raise AssertionError(f"unexpected GET: {url}")


def test_openai_http_client_uses_consistent_chrome120_headers() -> None:
    client = OpenAIHTTPClient()

    assert RequestConfig().impersonate == "chrome120"
    assert client.default_headers["User-Agent"] == (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    assert client.default_headers["sec-ch-ua"] == '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"'
    assert client.default_headers["sec-ch-ua-mobile"] == "?0"
    assert client.default_headers["sec-ch-ua-platform"] == '"Windows"'


def test_oauth_json_headers_include_client_hints() -> None:
    engine = RegistrationEngine(email_service=DummyEmailService())

    headers = engine._oauth_json_headers(
        referer="https://auth.openai.com/u/signup",
        device_id="did-123",
    )

    assert headers["user-agent"] == (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    assert headers["sec-ch-ua"] == '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"'
    assert headers["sec-ch-ua-mobile"] == "?0"
    assert headers["sec-ch-ua-platform"] == '"Windows"'


def test_create_user_account_logs_continue_kind_and_page_type(monkeypatch) -> None:
    class CreateAccountSession:
        def post(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
            del kwargs
            assert url.endswith("/create_account")
            return FakeResponse(
                200,
                url=url,
                json_data={
                    "continue_url": "https://chatgpt.com/api/auth/callback/openai?code=ac_demo&state=demo",
                    "page": {"type": "external_url"},
                },
            )

    monkeypatch.setattr(
        register_module.register_http_module,
        "generate_random_user_info",
        lambda: {"name": "Test", "birthdate": "1990-01-01"},
    )
    engine = RegistrationEngine(email_service=DummyEmailService())
    engine.session = CreateAccountSession()

    assert engine._create_user_account() is True
    joined_logs = "\n".join(engine.logs)
    assert "page_type=external_url" in joined_logs
    assert "continue_kind=callback_openai" in joined_logs
    assert "continue_host=chatgpt.com" in joined_logs


def test_login_for_token_uses_password_verify_and_workspace_flow(monkeypatch) -> None:
    fake_session = FakeSession()
    created_clients: list[object] = []
    submit_calls: list[dict[str, object]] = []

    class FakeOpenAIHTTPClient:
        def __init__(self, proxy_url=None):  # type: ignore[no-untyped-def]
            self.proxy_url = proxy_url
            self.session = fake_session
            self.default_headers = {"User-Agent": "FakeAgent/1.0"}
            self.sentinel_calls: list[tuple[str, str]] = []
            created_clients.append(self)

        def check_sentinel(self, did: str, *, flow: str = "authorize_continue") -> str:
            self.sentinel_calls.append((did, flow))
            return f"sentinel-{flow}"

    class FakeOAuthManager:
        def start_oauth(self) -> OAuthStart:
            return OAuthStart(
                auth_url="https://auth.openai.com/oauth/authorize?client_id=demo",
                state="demo-state",
                code_verifier="demo-verifier",
                redirect_uri="http://localhost:1455/auth/callback",
            )

    def fake_submit_callback_url(**kwargs):  # type: ignore[no-untyped-def]
        submit_calls.append(kwargs)
        return json.dumps(
            {
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "id_token": "id-token",
                "account_id": "acct-123",
                "email": "user@example.com",
                "expired": "2026-03-22T00:00:00Z",
                "last_refresh": "2026-03-21T00:00:00Z",
            }
        )

    monkeypatch.setattr(register_module, "OpenAIHTTPClient", FakeOpenAIHTTPClient)
    monkeypatch.setattr(register_module, "submit_callback_url", fake_submit_callback_url)

    engine = RegistrationEngine(email_service=DummyEmailService())
    engine.email = "user@example.com"
    engine.password = "pw-secret"
    engine.oauth_manager = FakeOAuthManager()

    result = engine._login_for_token()

    assert result is not None
    assert result["access_token"] == "access-token"
    assert result["refresh_token"] == "refresh-token"
    assert result["account_id"] == "acct-123"
    assert created_clients[-1].sentinel_calls == [  # type: ignore[attr-defined]
        ("did-123", "authorize_continue"),
        ("did-123", "password_verify"),
    ]

    authorize_call = next(
        call for call in fake_session.calls if call[0] == "POST" and call[1].endswith("/authorize/continue")
    )
    authorize_header = json.loads(authorize_call[2]["headers"]["openai-sentinel-token"])  # type: ignore[index]
    assert authorize_call[2]["json"] == {"username": {"kind": "email", "value": "user@example.com"}}  # type: ignore[index]
    assert authorize_header["flow"] == "authorize_continue"
    assert authorize_header["c"] == "sentinel-authorize_continue"

    password_call = next(
        call for call in fake_session.calls if call[0] == "POST" and call[1].endswith("/password/verify")
    )
    password_header = json.loads(password_call[2]["headers"]["openai-sentinel-token"])  # type: ignore[index]
    assert password_call[2]["json"] == {"password": "pw-secret"}  # type: ignore[index]
    assert password_header["flow"] == "password_verify"
    assert password_header["c"] == "sentinel-password_verify"

    workspace_call = next(
        call for call in fake_session.calls if call[0] == "POST" and call[1].endswith("/workspace/select")
    )
    assert workspace_call[2]["json"] == {"workspace_id": "ws-123"}  # type: ignore[index]

    organization_call = next(
        call for call in fake_session.calls if call[0] == "POST" and call[1].endswith("/organization/select")
    )
    assert organization_call[2]["json"] == {"org_id": "org-456", "project_id": "proj-789"}  # type: ignore[index]

    assert submit_calls[0]["callback_url"] == "http://localhost:1455/auth/callback?code=oauth-code&state=demo-state"
    assert submit_calls[0]["expected_state"] == "demo-state"
    assert submit_calls[0]["code_verifier"] == "demo-verifier"


def test_follow_redirects_with_session_extracts_localhost_callback_from_exception() -> None:
    class ErrorSession:
        def get(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
            del url, kwargs
            raise RuntimeError(
                "connection refused for http://localhost:1455/auth/callback?code=abc123&state=state456"
            )

    engine = RegistrationEngine(email_service=DummyEmailService())

    callback_url = engine._follow_redirects_with_session(ErrorSession(), "https://auth.openai.com/continue")

    assert callback_url == "http://localhost:1455/auth/callback?code=abc123&state=state456"


def test_login_for_token_retries_transient_password_verify_transport_error(monkeypatch) -> None:
    submit_calls: list[dict[str, object]] = []
    transport_failures = {"remaining": 1}

    class RetrySession(FakeSession):
        def __init__(self) -> None:
            super().__init__()

        def post(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
            if url.endswith("/password/verify"):
                if transport_failures["remaining"] > 0:
                    transport_failures["remaining"] -= 1
                    raise RuntimeError(
                        "Failed to perform, curl: (7) Connection closed abruptly. "
                        "See https://curl.se/libcurl/c/libcurl-errors.html first for more details."
                    )
            return super().post(url, **kwargs)

    session_instances: list[RetrySession] = []

    class FakeOpenAIHTTPClient:
        def __init__(self, proxy_url=None):  # type: ignore[no-untyped-def]
            del proxy_url
            self.session = RetrySession()
            self.default_headers = {"User-Agent": "FakeAgent/1.0"}
            session_instances.append(self.session)

        def check_sentinel(self, did: str, *, flow: str = "authorize_continue") -> str:
            del did
            return f"sentinel-{flow}"

        def close(self) -> None:
            return None

    class FakeOAuthManager:
        def start_oauth(self) -> OAuthStart:
            return OAuthStart(
                auth_url="https://auth.openai.com/oauth/authorize?client_id=demo",
                state="demo-state",
                code_verifier="demo-verifier",
                redirect_uri="http://localhost:1455/auth/callback",
            )

    def fake_submit_callback_url(**kwargs):  # type: ignore[no-untyped-def]
        submit_calls.append(kwargs)
        return json.dumps(
            {
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "id_token": "id-token",
                "account_id": "acct-123",
                "email": "user@example.com",
                "expired": "2026-03-22T00:00:00Z",
                "last_refresh": "2026-03-21T00:00:00Z",
            }
        )

    monkeypatch.setattr(register_module, "OpenAIHTTPClient", FakeOpenAIHTTPClient)
    monkeypatch.setattr(register_module, "submit_callback_url", fake_submit_callback_url)

    engine = RegistrationEngine(email_service=DummyEmailService())
    engine.email = "user@example.com"
    engine.password = "pw-secret"
    engine.oauth_manager = FakeOAuthManager()

    result = engine._login_for_token()

    assert result is not None
    assert result["access_token"] == "access-token"
    assert len(session_instances) >= 2
    assert any("transient transport error" in line for line in engine.logs)
    assert submit_calls[0]["callback_url"] == "http://localhost:1455/auth/callback?code=oauth-code&state=demo-state"


def test_login_for_token_uses_session_refresh_when_callback_missing(monkeypatch) -> None:
    class SessionTokenSession(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.cookies["__Secure-next-auth.session-token"] = "sess-123"

        def get(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
            self.calls.append(("GET", url, kwargs))
            if url.startswith("https://auth.openai.com/oauth/authorize"):
                self.cookies["oai-did"] = "did-123"
                self.cookies["login_session"] = "login-session"
                return FakeResponse(200, url="https://auth.openai.com/log-in")
            if url == "https://auth.openai.com/organization-continue":
                return FakeResponse(403, url=url, text="phone required")
            raise AssertionError(f"unexpected GET: {url}")

        def post(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
            self.calls.append(("POST", url, kwargs))
            if url.endswith("/authorize/continue"):
                return FakeResponse(
                    200,
                    url=url,
                    json_data={"continue_url": "/log-in/password", "page": {"type": "password"}},
                )
            if url.endswith("/password/verify"):
                return FakeResponse(
                    200,
                    url=url,
                    json_data={"continue_url": "/continue-after-password", "page": {"type": "consent"}},
                )
            if url.endswith("/workspace/select"):
                return FakeResponse(
                    200,
                    url=url,
                    json_data={
                        "continue_url": "/organization-continue",
                        "data": {"orgs": []},
                    },
                )
            raise AssertionError(f"unexpected POST: {url}")

    fake_session = SessionTokenSession()

    class FakeOpenAIHTTPClient:
        def __init__(self, proxy_url=None):  # type: ignore[no-untyped-def]
            self.proxy_url = proxy_url
            self.session = fake_session
            self.default_headers = {"User-Agent": "FakeAgent/1.0"}

        def check_sentinel(self, did: str, *, flow: str = "authorize_continue") -> str:
            del did
            return f"sentinel-{flow}"

        def close(self) -> None:
            return None

    class FakeOAuthManager:
        def start_oauth(self) -> OAuthStart:
            return OAuthStart(
                auth_url="https://auth.openai.com/oauth/authorize?client_id=demo",
                state="demo-state",
                code_verifier="demo-verifier",
                redirect_uri="http://localhost:1455/auth/callback",
            )

    class FakeTokenRefreshManager:
        def __init__(self, proxy_url=None):  # type: ignore[no-untyped-def]
            del proxy_url

        def refresh_by_session_token(self, session_token: str):  # type: ignore[no-untyped-def]
            assert session_token == "sess-123"
            return TokenRefreshResult(
                success=True,
                access_token="session-access",
                account_id="acct-session",
                email="user@example.com",
                session_token=session_token,
            )

    monkeypatch.setattr(register_module, "OpenAIHTTPClient", FakeOpenAIHTTPClient)
    monkeypatch.setattr(register_module, "TokenRefreshManager", FakeTokenRefreshManager)

    engine = RegistrationEngine(email_service=DummyEmailService())
    engine.email = "user@example.com"
    engine.password = "pw-secret"
    engine.oauth_manager = FakeOAuthManager()

    result = engine._login_for_token()

    assert result is not None
    assert result["access_token"] == "session-access"
    assert result["session_token"] == "sess-123"
    assert result["account_id"] == "acct-session"
    assert any("session token refresh succeeded" in line for line in engine.logs)


def test_get_workspace_id_no_longer_calls_invalid_workspaces_api() -> None:
    engine = RegistrationEngine(email_service=DummyEmailService())
    session = NoWorkspaceSession()
    engine.session = session

    assert engine._get_workspace_id() is None
    assert all("/api/accounts/workspaces" not in url for _, url, _ in session.calls)


def test_run_continues_token_acquisition_after_add_phone_continue_url(monkeypatch) -> None:
    engine = RegistrationEngine(email_service=DummyEmailService())
    engine.email = "user@example.com"
    engine.password = "pw-secret"

    monkeypatch.setattr(engine, "_check_ip_location", lambda: (True, "US"))
    monkeypatch.setattr(engine, "_create_email", lambda: True)
    monkeypatch.setattr(engine, "_init_session", lambda: True)
    monkeypatch.setattr(engine, "_start_oauth", lambda: True)
    monkeypatch.setattr(engine, "_get_device_id", lambda: "did-123")
    monkeypatch.setattr(engine, "_check_sentinel", lambda device_id: "sentinel")
    monkeypatch.setattr(engine, "_submit_signup_form", lambda device_id, sentinel_token: SignupFormResult(success=True))
    monkeypatch.setattr(engine, "_register_password", lambda: True)
    monkeypatch.setattr(engine, "_send_verification_code", lambda: True)
    monkeypatch.setattr(engine, "_get_verification_code", lambda: "123456")
    monkeypatch.setattr(engine, "_validate_verification_code", lambda code: True)

    def fake_create_user_account() -> bool:
        engine._create_account_continue_url = "https://auth.openai.com/add-phone"
        return True

    monkeypatch.setattr(engine, "_create_user_account", fake_create_user_account)
    monkeypatch.setattr(
        engine,
        "_login_for_token",
        lambda: {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "id_token": "id-token",
            "account_id": "acct-123",
            "expired": "2026-03-23T00:00:00Z",
            "last_refresh": "2026-03-22T00:00:00Z",
        },
    )

    result = engine.run()

    assert result.success is True
    assert result.stage == "completed"
    assert result.metadata["post_create_gate"] == "add_phone"
    assert result.metadata["post_create_continue_url"] == "https://auth.openai.com/add-phone"


def test_run_returns_add_phone_gate_when_token_acquisition_still_fails(monkeypatch) -> None:
    engine = RegistrationEngine(email_service=DummyEmailService())
    engine.email = "user@example.com"
    engine.password = "pw-secret"

    monkeypatch.setattr(engine, "_check_ip_location", lambda: (True, "US"))
    monkeypatch.setattr(engine, "_create_email", lambda: True)
    monkeypatch.setattr(engine, "_init_session", lambda: True)
    monkeypatch.setattr(engine, "_start_oauth", lambda: True)
    monkeypatch.setattr(engine, "_get_device_id", lambda: "did-123")
    monkeypatch.setattr(engine, "_check_sentinel", lambda device_id: "sentinel")
    monkeypatch.setattr(engine, "_submit_signup_form", lambda device_id, sentinel_token: SignupFormResult(success=True))
    monkeypatch.setattr(engine, "_register_password", lambda: True)
    monkeypatch.setattr(engine, "_send_verification_code", lambda: True)
    monkeypatch.setattr(engine, "_get_verification_code", lambda: "123456")
    monkeypatch.setattr(engine, "_validate_verification_code", lambda code: True)

    def fake_create_user_account() -> bool:
        engine._create_account_continue_url = "https://auth.openai.com/add-phone"
        return True

    monkeypatch.setattr(engine, "_create_user_account", fake_create_user_account)
    monkeypatch.setattr(engine, "_login_for_token", lambda: None)

    result = engine.run()

    assert result.success is False
    assert result.stage == "add_phone_gate"
    assert result.metadata["post_create_gate"] == "add_phone"
    assert result.metadata["post_create_continue_url"] == "https://auth.openai.com/add-phone"


def test_run_retries_add_phone_oauth_once_more_before_failure(monkeypatch) -> None:
    engine = RegistrationEngine(email_service=DummyEmailService())
    engine.email = "user@example.com"
    engine.password = "pw-secret"

    monkeypatch.setattr(engine, "_check_ip_location", lambda: (True, "US"))
    monkeypatch.setattr(engine, "_create_email", lambda: True)
    monkeypatch.setattr(engine, "_init_session", lambda: True)
    monkeypatch.setattr(engine, "_start_oauth", lambda: True)
    monkeypatch.setattr(engine, "_get_device_id", lambda: "did-123")
    monkeypatch.setattr(engine, "_check_sentinel", lambda device_id: "sentinel")
    monkeypatch.setattr(engine, "_submit_signup_form", lambda device_id, sentinel_token: SignupFormResult(success=True))
    monkeypatch.setattr(engine, "_register_password", lambda: True)
    monkeypatch.setattr(engine, "_send_verification_code", lambda: True)
    monkeypatch.setattr(engine, "_get_verification_code", lambda: "123456")
    monkeypatch.setattr(engine, "_validate_verification_code", lambda code: True)

    def fake_create_user_account() -> bool:
        engine._create_account_continue_url = "https://auth.openai.com/add-phone"
        return True

    monkeypatch.setattr(engine, "_create_user_account", fake_create_user_account)
    engine._add_phone_oauth_max_attempts = 2
    attempts = {"count": 0}

    def fake_login_for_token() -> None:
        attempts["count"] += 1
        return None

    monkeypatch.setattr(engine, "_login_for_token", fake_login_for_token)

    result = engine.run()

    assert result.success is False
    assert result.stage == "add_phone_gate"
    assert attempts["count"] == 2


def test_run_uses_direct_session_token_before_fresh_login_for_add_phone(monkeypatch) -> None:
    engine = RegistrationEngine(email_service=DummyEmailService())
    engine.email = "user@example.com"
    engine.password = "pw-secret"

    monkeypatch.setattr(engine, "_check_ip_location", lambda: (True, "US"))
    monkeypatch.setattr(engine, "_create_email", lambda: True)
    monkeypatch.setattr(engine, "_init_session", lambda: True)
    monkeypatch.setattr(engine, "_start_oauth", lambda: True)
    monkeypatch.setattr(engine, "_get_device_id", lambda: "did-123")
    monkeypatch.setattr(engine, "_check_sentinel", lambda device_id: "sentinel")
    monkeypatch.setattr(engine, "_submit_signup_form", lambda device_id, sentinel_token: SignupFormResult(success=True))
    monkeypatch.setattr(engine, "_register_password", lambda: True)
    monkeypatch.setattr(engine, "_send_verification_code", lambda: True)
    monkeypatch.setattr(engine, "_get_verification_code", lambda: "123456")
    monkeypatch.setattr(engine, "_validate_verification_code", lambda code: True)

    def fake_create_user_account() -> bool:
        engine._create_account_continue_url = "https://auth.openai.com/add-phone"
        return True

    monkeypatch.setattr(engine, "_create_user_account", fake_create_user_account)
    monkeypatch.setattr(
        engine,
        "_try_direct_session_token",
        lambda: {
            "access_token": "direct-access-token",
            "refresh_token": "direct-refresh-token",
            "id_token": "",
            "account_id": "acct-direct",
            "expired": "2026-03-30T00:00:00Z",
            "last_refresh": "2026-03-29T20:00:00Z",
        },
    )
    monkeypatch.setattr(
        engine,
        "_login_for_token",
        lambda: (_ for _ in ()).throw(AssertionError("fresh login fallback should not run when direct session token works")),
    )

    result = engine.run()

    assert result.success is True
    assert result.stage == "completed"
    assert result.account_id == "acct-direct"
    assert result.access_token == "direct-access-token"
    assert result.metadata["post_create_gate"] == "add_phone"


def test_run_uses_create_account_callback_session_before_workspace_or_fresh_login(monkeypatch) -> None:
    class CallbackSession:
        def __init__(self) -> None:
            self.cookies = FakeCookies({})
            self.calls: list[tuple[str, str, dict[str, object]]] = []

        def get(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
            self.calls.append(("GET", url, kwargs))
            if url.startswith("https://chatgpt.com/api/auth/callback/openai?code="):
                return FakeResponse(302, url=url, headers={"Location": "https://chatgpt.com/"})
            if url == "https://chatgpt.com/api/auth/session":
                return FakeResponse(
                    200,
                    url=url,
                    json_data={
                        "accessToken": "header.payload.sig",
                        "user": {"email": "user@example.com"},
                        "expires": "2026-03-30T00:00:00Z",
                    },
                )
            raise AssertionError(f"unexpected GET: {url}")

    engine = RegistrationEngine(email_service=DummyEmailService())
    engine.email = "user@example.com"
    engine.password = "pw-secret"
    engine.session = CallbackSession()

    monkeypatch.setattr(engine, "_check_ip_location", lambda: (True, "US"))
    monkeypatch.setattr(engine, "_create_email", lambda: True)
    monkeypatch.setattr(engine, "_init_session", lambda: True)
    monkeypatch.setattr(engine, "_start_oauth", lambda: True)
    monkeypatch.setattr(engine, "_get_device_id", lambda: "did-123")
    monkeypatch.setattr(engine, "_check_sentinel", lambda device_id: "sentinel")
    monkeypatch.setattr(engine, "_submit_signup_form", lambda device_id, sentinel_token: SignupFormResult(success=True))
    monkeypatch.setattr(engine, "_register_password", lambda: True)
    monkeypatch.setattr(engine, "_send_verification_code", lambda: True)
    monkeypatch.setattr(engine, "_get_verification_code", lambda: "123456")
    monkeypatch.setattr(engine, "_validate_verification_code", lambda code: True)

    def fake_create_user_account() -> bool:
        engine._create_account_continue_url = (
            "https://chatgpt.com/api/auth/callback/openai?code=oauth-code&state=demo-state"
        )
        return True

    monkeypatch.setattr(engine, "_create_user_account", fake_create_user_account)
    monkeypatch.setattr(
        engine,
        "_get_workspace_id",
        lambda: (_ for _ in ()).throw(AssertionError("workspace flow should not run when create_account already returned callback")),
    )
    monkeypatch.setattr(
        engine,
        "_login_for_token",
        lambda: (_ for _ in ()).throw(AssertionError("fresh login fallback should not run when callback session path works")),
    )
    monkeypatch.setattr(
        engine,
        "_parse_session_jwt",
        lambda access_token, session_data: {
            "access_token": access_token,
            "refresh_token": "",
            "id_token": "",
            "account_id": "acct-callback",
            "email": "user@example.com",
            "expired": "2026-03-30T00:00:00Z",
            "last_refresh": "2026-03-29T20:00:00Z",
            "source": "create_account_callback_session",
        },
    )

    result = engine.run()

    assert result.success is True
    assert result.stage == "completed"
    assert result.account_id == "acct-callback"
    assert result.access_token == "header.payload.sig"
    assert result.metadata["post_create_continue_url"] == (
        "https://chatgpt.com/api/auth/callback/openai?code=oauth-code&state=demo-state"
    )
    assert [url for method, url, _ in engine.session.calls if method == "GET"][-2:] == [
        "https://chatgpt.com/api/auth/callback/openai?code=oauth-code&state=demo-state",
        "https://chatgpt.com/api/auth/session",
    ]


def test_run_does_not_retry_non_add_phone_oauth_failure(monkeypatch) -> None:
    engine = RegistrationEngine(email_service=DummyEmailService())
    engine.email = "user@example.com"
    engine.password = "pw-secret"

    monkeypatch.setattr(engine, "_check_ip_location", lambda: (True, "US"))
    monkeypatch.setattr(engine, "_create_email", lambda: True)
    monkeypatch.setattr(engine, "_init_session", lambda: True)
    monkeypatch.setattr(engine, "_start_oauth", lambda: True)
    monkeypatch.setattr(engine, "_get_device_id", lambda: "did-123")
    monkeypatch.setattr(engine, "_check_sentinel", lambda device_id: "sentinel")
    monkeypatch.setattr(engine, "_submit_signup_form", lambda device_id, sentinel_token: SignupFormResult(success=True))
    monkeypatch.setattr(engine, "_register_password", lambda: True)
    monkeypatch.setattr(engine, "_send_verification_code", lambda: True)
    monkeypatch.setattr(engine, "_get_verification_code", lambda: "123456")
    monkeypatch.setattr(engine, "_validate_verification_code", lambda code: True)
    monkeypatch.setattr(engine, "_create_user_account", lambda: True)
    engine._add_phone_oauth_max_attempts = 2
    attempts = {"count": 0}

    def fake_login_for_token() -> None:
        attempts["count"] += 1
        return None

    monkeypatch.setattr(engine, "_login_for_token", fake_login_for_token)

    result = engine.run()

    assert result.success is False
    assert result.stage == "token_acquisition"
    assert attempts["count"] == 1


def test_login_for_token_uses_configured_add_phone_oauth_otp_timeout(monkeypatch) -> None:
    class OAuthOtpSession(FakeSession):
        def post(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
            self.calls.append(("POST", url, kwargs))
            if url.endswith("/authorize/continue"):
                return FakeResponse(
                    200,
                    url=url,
                    json_data={"continue_url": "/log-in/password", "page": {"type": "password"}},
                )
            if url.endswith("/password/verify"):
                return FakeResponse(
                    200,
                    url=url,
                    json_data={
                        "continue_url": "https://auth.openai.com/email-verification",
                        "page": {"type": register_module.OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]},
                    },
                )
            raise AssertionError(f"unexpected POST: {url}")

    fake_session = OAuthOtpSession()

    class FakeOpenAIHTTPClient:
        def __init__(self, proxy_url=None):  # type: ignore[no-untyped-def]
            self.proxy_url = proxy_url
            self.session = fake_session
            self.default_headers = {"User-Agent": "FakeAgent/1.0"}

        def check_sentinel(self, did: str, *, flow: str = "authorize_continue") -> str:
            del did, flow
            return "sentinel"

    class FakeOAuthManager:
        def start_oauth(self) -> OAuthStart:
            return OAuthStart(
                auth_url="https://auth.openai.com/oauth/authorize?client_id=demo",
                state="demo-state",
                code_verifier="demo-verifier",
                redirect_uri="http://localhost:1455/auth/callback",
            )

    observed: dict[str, object] = {}

    monkeypatch.setattr(register_module, "OpenAIHTTPClient", FakeOpenAIHTTPClient)
    monkeypatch.setenv("ZHUCE6_ADD_PHONE_OAUTH_OTP_TIMEOUT_SECONDS", "90")

    engine = RegistrationEngine(email_service=DummyEmailService())
    engine.email = "user@example.com"
    engine.password = "pw-secret"
    engine.oauth_manager = FakeOAuthManager()

    def fake_wait_for_mailbox_code(*, before_ids=None, timeout=0, keyword="", not_before_timestamp=None):  # type: ignore[no-untyped-def]
        observed["before_ids"] = before_ids
        observed["timeout"] = timeout
        observed["keyword"] = keyword
        observed["not_before_timestamp"] = not_before_timestamp
        return ""

    monkeypatch.setattr(engine, "_wait_for_mailbox_code", fake_wait_for_mailbox_code)

    result = engine._login_for_token()

    assert result is None
    assert not any(call[0] == "GET" and call[1].endswith("/api/accounts/email-otp/send") for call in fake_session.calls)
    assert observed["timeout"] == 90
    assert observed["keyword"] == "openai"
    assert observed["not_before_timestamp"] is None


def test_build_sentinel_header_prefers_client_pow_payload() -> None:
    class FakePowClient:
        def build_sentinel_header(self, *, device_id: str, flow: str, token: str = "") -> str:
            return json.dumps(
                {
                    "p": "gAAAAABpowtoken",
                    "t": "",
                    "c": token,
                    "id": device_id,
                    "flow": flow,
                },
                separators=(",", ":"),
            )

    engine = RegistrationEngine(email_service=DummyEmailService())

    header = json.loads(
        engine._build_sentinel_header(
            "sentinel-authorize_continue",
            "did-123",
            "authorize_continue",
            client=FakePowClient(),
        )
    )

    assert header["p"] == "gAAAAABpowtoken"
    assert header["c"] == "sentinel-authorize_continue"
    assert header["flow"] == "authorize_continue"


def test_build_sentinel_header_falls_back_when_client_has_no_pow_helper() -> None:
    engine = RegistrationEngine(email_service=DummyEmailService())

    header = json.loads(engine._build_sentinel_header("sentinel-basic", "did-123", "authorize_continue"))

    assert header["p"] == ""
    assert header["t"] == ""
    assert header["c"] == "sentinel-basic"


def test_create_email_discards_duplicate_mailboxes() -> None:
    class DuplicateEmailService:
        def __init__(self) -> None:
            self.calls = 0

        def create_email(self, config=None):  # type: ignore[no-untyped-def]
            del config
            self.calls += 1
            if self.calls == 1:
                return {"email": "dup@example.com"}
            return {"email": "fresh@example.com"}

        def get_verification_code(self, **kwargs):  # type: ignore[no-untyped-def]
            del kwargs
            return ""

    class FakeDedupeStore:
        def __init__(self) -> None:
            self.reserved: list[str] = []

        def reserve(self, email: str) -> bool:
            if email == "dup@example.com":
                return False
            self.reserved.append(email)
            return True

        def release(self, email: str) -> None:
            del email

        def mark(self, email: str, *, reason: str) -> None:
            del email, reason

    service = DuplicateEmailService()
    engine = RegistrationEngine(
        email_service=service,
        mailbox_dedupe_store=FakeDedupeStore(),
        create_email_max_attempts=2,
    )

    assert engine._create_email() is True
    assert engine.email == "fresh@example.com"
    assert service.calls == 2
    assert any("duplicate mailbox discarded" in line for line in engine.logs)


def test_run_marks_user_already_exists_mailbox(monkeypatch) -> None:
    class FakeDedupeStore:
        def __init__(self) -> None:
            self.marked: list[tuple[str, str]] = []
            self.released: list[str] = []

        def reserve(self, email: str) -> bool:
            return True

        def release(self, email: str) -> None:
            self.released.append(email)

        def mark(self, email: str, *, reason: str) -> None:
            self.marked.append((email, reason))

    dedupe_store = FakeDedupeStore()
    engine = RegistrationEngine(email_service=DummyEmailService(), mailbox_dedupe_store=dedupe_store)
    engine.email = "dup@example.com"
    engine._reserved_email = "dup@example.com"

    monkeypatch.setattr(engine, "_check_ip_location", lambda: (True, "SG"))
    monkeypatch.setattr(engine, "_create_email", lambda: True)
    monkeypatch.setattr(engine, "_init_session", lambda: True)
    monkeypatch.setattr(engine, "_start_oauth", lambda: True)
    monkeypatch.setattr(engine, "_get_device_id", lambda: "did-123")
    monkeypatch.setattr(engine, "_check_sentinel", lambda device_id: "sentinel")
    monkeypatch.setattr(engine, "_submit_signup_form", lambda device_id, sentinel_token: SignupFormResult(success=True))
    monkeypatch.setattr(engine, "_register_password", lambda: True)
    monkeypatch.setattr(engine, "_send_verification_code", lambda: True)
    monkeypatch.setattr(engine, "_get_verification_code", lambda: "123456")
    monkeypatch.setattr(engine, "_validate_verification_code", lambda code: True)

    def fake_create_user_account() -> bool:
        engine._last_create_account_error_code = "user_already_exists"
        engine._last_create_account_error_message = "An account already exists for this email address."
        return False

    monkeypatch.setattr(engine, "_create_user_account", fake_create_user_account)

    result = engine.run()

    assert result.success is False
    assert result.stage == "create_account"
    assert dedupe_store.marked == [("dup@example.com", "user_already_exists")]
    assert dedupe_store.released == ["dup@example.com"]


def test_create_user_account_classifies_registration_disallowed() -> None:
    class CreateAccountSession:
        def post(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
            del kwargs
            assert url.endswith("/create_account")
            return FakeResponse(
                400,
                json_data={
                    "error": {
                        "message": "Sorry, we cannot create your account with the given information.",
                        "code": "registration_disallowed",
                    }
                },
            )

    engine = RegistrationEngine(email_service=DummyEmailService())
    engine.email = "demo@nova.example.test"
    engine.session = CreateAccountSession()

    success = engine._create_user_account()
    result = engine._result(success=False, stage="create_account", error_message="create account failed")

    assert success is False
    assert result.metadata["email_domain"] == "nova.example.test"
    assert result.metadata["create_account_http_status"] == 400
    assert result.metadata["create_account_error_code"] == "registration_disallowed"
    assert "cannot create your account" in result.metadata["create_account_error_message"]


def test_create_user_account_classifies_unsupported_email() -> None:
    class UnsupportedEmailSession:
        def post(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
            del kwargs
            assert url.endswith("/create_account")
            return FakeResponse(
                400,
                json_data={
                    "error": {
                        "message": "The email address is not supported.",
                        "code": "unsupported_email",
                    }
                },
            )

    engine = RegistrationEngine(email_service=DummyEmailService())
    engine.email = "demo@blacklisted.example.test"
    engine.session = UnsupportedEmailSession()

    success = engine._create_user_account()
    result = engine._result(success=False, stage="create_account", error_message="create account failed")

    assert success is False
    assert result.metadata["email_domain"] == "blacklisted.example.test"
    assert result.metadata["create_account_http_status"] == 400
    assert result.metadata["create_account_error_code"] == "unsupported_email"
    assert "not supported" in result.metadata["create_account_error_message"]


def test_get_verification_code_prefers_mailbox_context_and_logs_wait_diagnostics(monkeypatch) -> None:
    class FakeMailbox:
        def __init__(self) -> None:
            self.last_wait_diagnostics = {
                "first_message_seen_at": 103.0,
                "matched_message_at": 104.0,
                "poll_count": 2,
                "message_scan_count": 3,
            }
            self.calls = []

        def wait_for_code(self, account, *, keyword='', timeout=120, before_ids=None):  # type: ignore[no-untyped-def]
            self.calls.append({"account": account, "keyword": keyword, "timeout": timeout, "before_ids": before_ids})
            return '123456'

    fake_mailbox = FakeMailbox()
    fake_account = object()

    class FakeEmailService(DummyEmailService):
        def __init__(self) -> None:
            self.mailbox = fake_mailbox
            self._account = fake_account

    times = iter([105.0, 106.0])
    monkeypatch.setattr(register_module.time, 'time', lambda: next(times))
    monkeypatch.setenv('ZHUCE6_WAIT_OTP_TIMEOUT_SECONDS', '180')

    engine = RegistrationEngine(email_service=FakeEmailService())
    engine.email = 'demo@example.com'
    engine._otp_sent_at = 100.0
    engine._signup_otp_before_ids = {'old-1'}

    code = engine._get_verification_code()

    assert code == '123456'
    assert fake_mailbox.calls[0]['before_ids'] == {'old-1'}
    assert fake_mailbox.calls[0]['timeout'] == 180
    assert any('waiting for verification code via mailbox' in line for line in engine.logs)
    assert any('otp mailbox diagnostics' in line for line in engine.logs)


def test_get_verification_code_records_no_message_timeout_metadata(monkeypatch) -> None:
    class FakeMailbox:
        def __init__(self) -> None:
            self.last_wait_diagnostics = {
                "first_message_seen_at": None,
                "matched_message_at": None,
                "poll_count": 12,
                "message_scan_count": 0,
            }

        def wait_for_code(self, account, *, keyword='', timeout=120, before_ids=None):  # type: ignore[no-untyped-def]
            del account, keyword, timeout, before_ids
            return ''

    fake_mailbox = FakeMailbox()
    fake_account = object()

    class FakeEmailService(DummyEmailService):
        def __init__(self) -> None:
            self.mailbox = fake_mailbox
            self._account = fake_account

    times = iter([100.0, 101.0, 102.0])
    monkeypatch.setattr(register_module.time, 'time', lambda: next(times))

    engine = RegistrationEngine(email_service=FakeEmailService())
    engine.email = 'demo@example.com'
    engine._otp_sent_at = 99.0

    code = engine._get_verification_code()

    assert code is None
    metadata = engine._metadata()
    assert metadata["otp_wait_failure_reason"] == "mailbox_timeout_no_message"
    assert metadata["otp_mailbox_message_scan_count"] == 0

class _OtpRetrySession:
    def __init__(self, *, method: str, response: FakeResponse | None = None, exc: Exception | None = None) -> None:
        self.cookies = FakeCookies({"oai-client-auth-session": _workspace_cookie("ws-123")})
        self._method = method
        self._response = response
        self._exc = exc
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def get(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(("GET", url, kwargs))
        if self._method != "GET":
            raise AssertionError(f"unexpected GET: {url}")
        if self._exc is not None:
            raise self._exc
        assert self._response is not None
        return self._response

    def post(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(("POST", url, kwargs))
        if self._method != "POST":
            raise AssertionError(f"unexpected POST: {url}")
        if self._exc is not None:
            raise self._exc
        assert self._response is not None
        return self._response


class _OtpRetryHTTPClient:
    def __init__(self, sessions):  # type: ignore[no-untyped-def]
        self._sessions = list(sessions)
        self._index = 0
        self.default_headers = {"User-Agent": "FakeAgent/1.0"}

    @property
    def session(self):  # type: ignore[no-untyped-def]
        return self._sessions[self._index]

    def close(self) -> None:
        if self._index < len(self._sessions) - 1:
            self._index += 1


def test_send_verification_code_retries_transient_transport_error(monkeypatch) -> None:
    monkeypatch.setattr("platforms.chatgpt.register_http.time.sleep", lambda _: None)
    timeout_exc = RuntimeError(
        "Failed to perform, curl: (28) Operation timed out after 30000 milliseconds with 0 bytes received."
    )
    sessions = [
        _OtpRetrySession(method="GET", exc=timeout_exc),
        _OtpRetrySession(method="GET", response=FakeResponse(200, url="https://auth.openai.com/api/accounts/email-otp/send")),
    ]
    engine = RegistrationEngine(email_service=DummyEmailService())
    engine.http_client = _OtpRetryHTTPClient(sessions)
    engine.session = engine.http_client.session
    engine.email = "user@example.com"

    assert engine._send_verification_code() is True
    assert engine.http_client._index == 1
    assert any("send otp: transient transport error" in line for line in engine.logs)
    assert any("send otp status: 200" in line for line in engine.logs)



def test_validate_verification_code_retries_transient_transport_error(monkeypatch) -> None:
    monkeypatch.setattr("platforms.chatgpt.register_http.time.sleep", lambda _: None)
    timeout_exc = RuntimeError(
        "Failed to perform, curl: (28) Operation timed out after 30000 milliseconds with 0 bytes received."
    )
    sessions = [
        _OtpRetrySession(method="POST", exc=timeout_exc),
        _OtpRetrySession(method="POST", response=FakeResponse(200, url="https://auth.openai.com/api/accounts/email-otp/validate")),
    ]
    engine = RegistrationEngine(email_service=DummyEmailService())
    engine.http_client = _OtpRetryHTTPClient(sessions)
    engine.session = engine.http_client.session

    assert engine._validate_verification_code("123456") is True
    assert engine.http_client._index == 1
    assert any("validate otp: transient transport error" in line for line in engine.logs)
    assert any("validate otp status: 200" in line for line in engine.logs)

import base64
import json

from platforms.chatgpt.oauth import OAuthManager, generate_oauth_url


def _jwt_payload(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"header.{encoded}.sig"


def test_generate_oauth_url_contains_state_and_verifier() -> None:
    result = generate_oauth_url()
    assert result.auth_url.startswith("https://")
    assert result.state
    assert result.code_verifier


def test_handle_callback_extracts_token_fields(monkeypatch) -> None:
    id_token = _jwt_payload(
        {
            "email": "oauth@example.com",
            "https://api.openai.com/auth": {"chatgpt_account_id": "acct-123"},
        }
    )

    def fake_post_form(url, data, timeout=30, proxy_url=None):  # type: ignore[no-untyped-def]
        del url, data, timeout, proxy_url
        return {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "id_token": id_token,
            "expires_in": 3600,
        }

    monkeypatch.setattr("platforms.chatgpt.oauth._post_form", fake_post_form)
    result = OAuthManager().handle_callback(
        callback_url="http://localhost/callback?code=abc&state=demo-state",
        expected_state="demo-state",
        code_verifier="demo-verifier",
    )
    assert result["email"] == "oauth@example.com"
    assert result["account_id"] == "acct-123"
    assert result["access_token"] == "access-token"
    assert result["refresh_token"] == "refresh-token"

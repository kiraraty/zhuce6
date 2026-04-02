from types import SimpleNamespace

from platforms.chatgpt import cpa_upload as cpa_upload_module


def test_upload_to_team_manager_reuses_cpa_upload(monkeypatch) -> None:
    calls: list[tuple[dict[str, str], str, str]] = []

    def fake_upload_to_cpa(token_data, api_url=None, api_key=None, proxy=None):  # type: ignore[no-untyped-def]
        del proxy
        calls.append((token_data, api_url, api_key))
        return True, "upload success"

    monkeypatch.setattr("platforms.chatgpt.cpa_upload.upload_to_cpa", fake_upload_to_cpa)
    account = SimpleNamespace(
        email="team@example.com",
        expires_at=None,
        last_refresh=None,
        id_token="id-token",
        account_id="acct-1",
        access_token="access-token",
        refresh_token="refresh-token",
    )
    ok, message = cpa_upload_module.upload_to_team_manager(
        account,
        api_url="https://example.com",
        api_key="secret",
    )
    assert ok is True
    assert message == "team manager upload success"
    assert calls and calls[0][0]["email"] == "team@example.com"


def test_test_cpa_connection_requires_url() -> None:
    ok, message = cpa_upload_module.test_cpa_connection(api_url=None, api_key="secret")
    assert ok is False
    assert message == "CPA API URL is required"

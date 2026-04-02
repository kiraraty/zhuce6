import json

from platforms.chatgpt.http_client import OpenAIHTTPClient


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, object]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, object]:
        return self._payload


def test_check_sentinel_caches_pow_payload(monkeypatch) -> None:
    sent_requests: list[dict[str, object]] = []

    def fake_post(self, url, **kwargs):  # type: ignore[no-untyped-def]
        sent_requests.append({"url": url, **kwargs})
        return FakeResponse(
            200,
            {
                "token": "sentinel-token",
                "proofofwork": {
                    "required": True,
                    "seed": "seed-123",
                    "difficulty": "ffffffff",
                },
            },
        )

    monkeypatch.setattr(OpenAIHTTPClient, "post", fake_post)

    client = OpenAIHTTPClient()
    token = client.check_sentinel("did-123", flow="authorize_continue")
    header = json.loads(
        client.build_sentinel_header(
            device_id="did-123",
            flow="authorize_continue",
            token=token or "",
        )
    )

    assert token == "sentinel-token"
    assert sent_requests[0]["headers"]["sec-ch-ua-platform"] == '"Windows"'
    assert header["c"] == "sentinel-token"
    assert header["p"].startswith("gAAAAAB")
    assert header["flow"] == "authorize_continue"


def test_check_sentinel_uses_requirements_token_when_pow_not_required(monkeypatch) -> None:
    def fake_post(self, url, **kwargs):  # type: ignore[no-untyped-def]
        del url
        return FakeResponse(200, {"token": "sentinel-token", "proofofwork": {"required": False}})

    monkeypatch.setattr(OpenAIHTTPClient, "post", fake_post)

    client = OpenAIHTTPClient()
    token = client.check_sentinel("did-456", flow="password_verify")
    header = json.loads(
        client.build_sentinel_header(
            device_id="did-456",
            flow="password_verify",
            token=token or "",
        )
    )

    assert token == "sentinel-token"
    assert header["p"].startswith("gAAAAAC")
    assert header["flow"] == "password_verify"

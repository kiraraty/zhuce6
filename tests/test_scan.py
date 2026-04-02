from pathlib import Path
import json

from ops.scan import classify_token_file


def test_classify_token_file_retries_transient_transport_error_with_fresh_session(
    monkeypatch,
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "fresh@example.com.json"
    token_file.write_text(
        json.dumps({"access_token": "token-1", "account_id": "acct-1"}),
        encoding="utf-8",
    )

    class FailingSession:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            del args, kwargs
            self.calls += 1
            raise RuntimeError(
                "Failed to perform, curl: (35) TLS connect error: "
                "error:00000000:OPENSSL_internal:invalid library (0)."
            )

    class HealthySession:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            del args, kwargs
            self.calls += 1
            return type("Response", (), {"status_code": 200, "text": "{\"ok\":true}"})()

    sessions = [FailingSession(), HealthySession()]

    def fake_get_session():  # type: ignore[no-untyped-def]
        return sessions.pop(0)

    monkeypatch.setattr("ops.scan.get_session", fake_get_session)
    monkeypatch.setattr("ops.scan.reset_session", lambda: None)

    result = classify_token_file(token_file, proxy="http://127.0.0.1:7899", timeout=15)

    assert result.category == "normal"
    assert result.status_code == 200
    assert "ok" in result.detail


def test_classify_token_file_reports_missing_file_separately(tmp_path: Path) -> None:
    token_file = tmp_path / "missing@example.com.json"

    result = classify_token_file(token_file, proxy=None, timeout=15)

    assert result.category == "missing"
    assert result.status_code is None
    assert result.detail.startswith("missing_file:")


def test_classify_token_file_requires_responses_path_when_enabled(monkeypatch, tmp_path: Path) -> None:
    token_file = tmp_path / "service@example.com.json"
    token_file.write_text(
        json.dumps({"access_token": "token-1", "account_id": "acct-1"}),
        encoding="utf-8",
    )

    class Session:
        def get(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            del args, kwargs
            return type("Response", (), {"status_code": 200, "text": '{"ok":true}'})()

        def post(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            del args, kwargs
            return type("Response", (), {"status_code": 500, "text": '{"error":{"message":"unexpected EOF","type":"server_error"}}'})()

    monkeypatch.setattr("ops.scan.get_session", lambda: Session())

    result = classify_token_file(token_file, proxy=None, timeout=15, require_response_path=True)

    assert result.category == "service_error"
    assert result.status_code == 500
    assert "unexpected EOF" in result.detail

from pathlib import Path

from ops.scan import ScanResult


def _write_token(path: Path, *, email: str, created_at: str) -> None:
    path.write_text(
        (
            "{\n"
            f'  "email": "{email}",\n'
            '  "access_token": "tok",\n'
            '  "account_id": "acct",\n'
            f'  "created_at": "{created_at}"\n'
            "}\n"
        ),
        encoding="utf-8",
    )


def test_responses_survival_seeds_recent_cohort_and_records_first_401(monkeypatch, tmp_path: Path) -> None:
    from ops.responses_survival import responses_survival_once

    newer = tmp_path / "newer@example.com.json"
    older = tmp_path / "older@example.com.json"
    _write_token(newer, email="newer@example.com", created_at="2026-03-30T20:20:00+08:00")
    _write_token(older, email="older@example.com", created_at="2026-03-30T20:19:00+08:00")
    state_file = tmp_path / "responses_survival.json"

    def fake_probe(path, proxy, timeout):  # type: ignore[no-untyped-def]
        del proxy, timeout
        if path.name == newer.name:
            return ScanResult(file=path.name, category="normal", status_code=200, detail="responses_ok")
        return ScanResult(file=path.name, category="invalid", status_code=401, detail="unauthorized")

    monkeypatch.setattr("ops.responses_survival.probe_responses_token_file", fake_probe)

    result = responses_survival_once(
        pool_dir=tmp_path,
        state_file=state_file,
        cohort_size=2,
        proxy=None,
        timeout_seconds=30,
        reseed=True,
    )

    assert result["probe_mode"] == "responses"
    assert result["seeded"] is True
    assert [member["email"] for member in result["members"]] == ["newer@example.com", "older@example.com"]
    assert result["summary"]["tracked"] == 2
    assert result["summary"]["alive"] == 1
    assert result["summary"]["invalid"] == 1
    invalid_member = next(member for member in result["members"] if member["email"] == "older@example.com")
    assert invalid_member["first_invalid_at"]
    assert invalid_member["survival_seconds"] is not None
    assert invalid_member["survival_seconds"] >= 0
    assert {item["to"] for item in result["changes"]} == {"normal", "invalid"}

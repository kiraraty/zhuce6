from pathlib import Path

from ops.account_survival import account_survival_once
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


def test_account_survival_seeds_fixed_recent_cohort_and_records_first_401(monkeypatch, tmp_path: Path) -> None:
    newest = tmp_path / "newest@example.com.json"
    older = tmp_path / "older@example.com.json"
    _write_token(newest, email="newest@example.com", created_at="2026-03-26T12:00:00+08:00")
    _write_token(older, email="older@example.com", created_at="2026-03-26T11:59:00+08:00")
    state_file = tmp_path / "account_survival.json"

    def fake_classify(path, proxy, timeout):  # type: ignore[no-untyped-def]
        del proxy, timeout
        if path.name == newest.name:
            return ScanResult(file=path.name, category="normal", status_code=200, detail="ok")
        return ScanResult(file=path.name, category="invalid", status_code=401, detail="401 invalidated")

    monkeypatch.setattr("ops.account_survival.classify_token_file", fake_classify)

    result = account_survival_once(
        pool_dir=tmp_path,
        state_file=state_file,
        cohort_size=2,
        proxy=None,
        timeout_seconds=15,
    )

    assert result["seeded"] is True
    assert [member["email"] for member in result["members"]] == ["newest@example.com", "older@example.com"]
    assert result["summary"]["tracked"] == 2
    assert result["summary"]["alive"] == 1
    assert result["summary"]["invalid"] == 1
    invalid_member = next(member for member in result["members"] if member["email"] == "older@example.com")
    assert invalid_member["first_invalid_at"]
    assert invalid_member["survival_seconds"] is not None
    assert invalid_member["survival_seconds"] >= 0


def test_account_survival_keeps_existing_fixed_members_without_reseed(monkeypatch, tmp_path: Path) -> None:
    tracked = tmp_path / "tracked@example.com.json"
    ignored = tmp_path / "ignored@example.com.json"
    _write_token(tracked, email="tracked@example.com", created_at="2026-03-26T12:00:00+08:00")
    _write_token(ignored, email="ignored@example.com", created_at="2026-03-26T12:01:00+08:00")
    state_file = tmp_path / "account_survival.json"
    state_file.write_text(
        (
            "{\n"
            f'  "state_file": "{state_file}",\n'
            f'  "pool_dir": "{tmp_path}",\n'
            '  "cohort_size": 1,\n'
            '  "members": [\n'
            "    {\n"
            '      "email": "tracked@example.com",\n'
            f'      "file_name": "{tracked.name}",\n'
            f'      "path": "{tracked}",\n'
            '      "created_at": "2026-03-26T12:00:00+08:00",\n'
            '      "selected_at": "2026-03-26T12:00:10+08:00",\n'
            '      "first_probe_at": "",\n'
            '      "last_probe_at": "",\n'
            '      "probe_count": 0,\n'
            '      "last_probe_status_code": null,\n'
            '      "last_probe_category": "",\n'
            '      "last_probe_detail": "",\n'
            '      "transport_error_count": 0,\n'
            '      "suspicious_count": 0,\n'
            '      "missing_at": "",\n'
            '      "first_invalid_at": "",\n'
            '      "survival_seconds": null,\n'
            '      "state": "tracking"\n'
            "    }\n"
            "  ]\n"
            "}\n"
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "ops.account_survival.classify_token_file",
        lambda path, proxy, timeout: ScanResult(file=path.name, category="normal", status_code=200, detail="ok"),
    )

    result = account_survival_once(
        pool_dir=tmp_path,
        state_file=state_file,
        cohort_size=1,
        proxy=None,
        timeout_seconds=15,
    )

    assert result["seeded"] is False
    assert [member["email"] for member in result["members"]] == ["tracked@example.com"]


def test_account_survival_reseed_replaces_members_with_latest_ten(monkeypatch, tmp_path: Path) -> None:
    for idx in range(12):
        _write_token(
            tmp_path / f"user{idx:02d}@example.com.json",
            email=f"user{idx:02d}@example.com",
            created_at=f"2026-03-26T12:{idx:02d}:00+08:00",
        )

    state_file = tmp_path / "account_survival.json"
    state_file.write_text(
        (
            "{\n"
            '  "seed_source": "recent_existing_pool_files",\n'
            '  "members": [\n'
            "    {\n"
            '      "email": "legacy@example.com",\n'
            '      "file_name": "legacy@example.com.json",\n'
            f'      "path": "{tmp_path / "legacy@example.com.json"}",\n'
            '      "created_at": "2026-03-25T12:00:00+08:00",\n'
            '      "selected_at": "2026-03-25T12:00:00+08:00",\n'
            '      "first_probe_at": "",\n'
            '      "last_probe_at": "",\n'
            '      "probe_count": 0,\n'
            '      "last_probe_status_code": null,\n'
            '      "last_probe_category": "",\n'
            '      "last_probe_detail": "",\n'
            '      "transport_error_count": 0,\n'
            '      "suspicious_count": 0,\n'
            '      "missing_at": "",\n'
            '      "first_invalid_at": "",\n'
            '      "survival_seconds": null,\n'
            '      "state": "tracking"\n'
            "    }\n"
            "  ]\n"
            "}\n"
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "ops.account_survival.classify_token_file",
        lambda path, proxy, timeout: ScanResult(file=path.name, category="normal", status_code=200, detail="ok"),
    )

    result = account_survival_once(
        pool_dir=tmp_path,
        state_file=state_file,
        cohort_size=10,
        proxy=None,
        timeout_seconds=15,
        reseed=True,
    )

    assert result["seeded"] is True
    assert result["reseeded"] is True
    assert result["seed_source"] == "latest_generated_pool_files"
    assert result["summary"]["tracked"] == 10
    assert [member["email"] for member in result["members"]] == [
        "user11@example.com",
        "user10@example.com",
        "user09@example.com",
        "user08@example.com",
        "user07@example.com",
        "user06@example.com",
        "user05@example.com",
        "user04@example.com",
        "user03@example.com",
        "user02@example.com",
    ]

from __future__ import annotations

from urllib.error import HTTPError

from ops import d1_cleanup


def _set_cloudflare_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ZHUCE6_CFMAIL_CF_AUTH_EMAIL", "user@example.com")
    monkeypatch.setenv("ZHUCE6_CFMAIL_CF_AUTH_KEY", "secret")
    monkeypatch.setenv("ZHUCE6_CFMAIL_CF_ACCOUNT_ID", "acct-123")


def test_d1_cleanup_skips_missing_credentials_only_warns_once(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("ZHUCE6_CFMAIL_CF_AUTH_EMAIL", raising=False)
    monkeypatch.delenv("ZHUCE6_CFMAIL_CF_AUTH_KEY", raising=False)
    monkeypatch.delenv("ZHUCE6_CFMAIL_CF_ACCOUNT_ID", raising=False)
    monkeypatch.setattr(d1_cleanup, "_missing_credentials_warned", False)

    first = d1_cleanup.d1_cleanup_once(database_id="db-1")
    second = d1_cleanup.d1_cleanup_once(database_id="db-1")

    captured = capsys.readouterr()
    assert first["skipped_reason"] == "missing_cloudflare_credentials"
    assert second["skipped_reason"] == "missing_cloudflare_credentials"
    assert captured.out.count("missing Cloudflare credentials") == 1


def test_d1_cleanup_skips_missing_database_id(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    _set_cloudflare_env(monkeypatch)
    monkeypatch.setattr(d1_cleanup, "_missing_credentials_warned", False)

    summary = d1_cleanup.d1_cleanup_once(database_id="")

    captured = capsys.readouterr()
    assert summary["skipped_reason"] == "missing_database_id"
    assert captured.out == ""


def test_delete_in_batches_loops_until_changes_zero(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _set_cloudflare_env(monkeypatch)
    monkeypatch.setattr(d1_cleanup, "_missing_credentials_warned", False)
    calls: list[tuple[str, str]] = []
    responses = iter(
        [
            ([], {"changes": 5000, "size_after": 9000}),
            ([], {"changes": 1200, "size_after": 7000}),
            ([], {"changes": 0, "size_after": 6800}),
        ]
    )

    def fake_query_once(database_id: str, sql: str, params=None):  # type: ignore[no-untyped-def]
        calls.append((database_id, sql))
        return next(responses)

    monkeypatch.setattr(d1_cleanup, "_query_once", fake_query_once)

    deleted, size_after = d1_cleanup._delete_in_batches("db-1", "raw_mails", 2, 5000)

    assert deleted == 6200
    assert size_after == 6800
    assert len(calls) == 3
    assert all(database_id == "db-1" for database_id, _sql in calls)
    assert all("DELETE FROM raw_mails" in sql for _database_id, sql in calls)
    assert all("datetime('now', '-2 hours')" in sql for _database_id, sql in calls)
    assert all("LIMIT 5000" in sql for _database_id, sql in calls)


def test_d1_cleanup_skips_when_counts_are_zero(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    _set_cloudflare_env(monkeypatch)
    monkeypatch.setattr(d1_cleanup, "_missing_credentials_warned", False)

    counts = {
        "raw_mails": (0, 1024),
        "address": (0, 2048),
    }
    monkeypatch.setattr(d1_cleanup, "_count_rows", lambda database_id, table: counts[table])
    monkeypatch.setattr(
        d1_cleanup,
        "_delete_in_batches",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("delete should not run")),
    )

    summary = d1_cleanup.d1_cleanup_once(database_id="db-1")

    captured = capsys.readouterr()
    assert summary["skipped_reason"] == "nothing_to_clean"
    assert summary["size_after_bytes"] == 2048
    assert "nothing to clean" in captured.out


def test_d1_cleanup_deletes_each_table_and_skips_missing_sender(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    _set_cloudflare_env(monkeypatch)
    monkeypatch.setattr(d1_cleanup, "_missing_credentials_warned", False)
    monkeypatch.setattr(
        d1_cleanup,
        "_count_rows",
        lambda database_id, table: (12, 4096) if table == "raw_mails" else (3, 4096),
    )
    calls: list[tuple[str, int, int]] = []

    def fake_delete_in_batches(database_id: str, table: str, retention_hours: int, batch_size: int):  # type: ignore[no-untyped-def]
        calls.append((table, retention_hours, batch_size))
        if table == "raw_mails":
            return 6200, 8192
        if table == "address":
            return 120, 6144
        raise d1_cleanup.D1TableMissingError("no such table: address_sender")

    monkeypatch.setattr(d1_cleanup, "_delete_in_batches", fake_delete_in_batches)
    monkeypatch.setattr(d1_cleanup, "_final_size_after", lambda database_id: 5120)

    summary = d1_cleanup.d1_cleanup_once(
        database_id="db-1",
        mail_retention_hours=2,
        address_retention_hours=24,
    )

    captured = capsys.readouterr()
    assert summary["deleted_mails"] == 6200
    assert summary["deleted_addresses"] == 120
    assert summary["deleted_senders"] == 0
    assert summary["size_after_bytes"] == 5120
    assert summary["skipped_reason"] is None
    assert calls == [
        ("raw_mails", 2, d1_cleanup.DEFAULT_D1_CLEANUP_BATCH_SIZE),
        ("address", 24, d1_cleanup.DEFAULT_D1_CLEANUP_BATCH_SIZE),
        ("address_sender", 24, d1_cleanup.DEFAULT_D1_CLEANUP_BATCH_SIZE),
    ]
    assert "table address_sender not found, skip cleanup" in captured.out
    assert "清理完成 | raw_mails=-6200 | address=-120 | address_sender=-0 | size=0.0MB" in captured.out


def test_query_converts_http_table_missing_into_d1_table_missing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _set_cloudflare_env(monkeypatch)
    monkeypatch.setattr(d1_cleanup, "_missing_credentials_warned", False)

    class FakeHttpResponse:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def read(self) -> bytes:
            return self._payload

        def close(self) -> None:
            return

    payload = (
        b'{"messages":[],"result":[],"success":false,'
        b'"errors":[{"code":7500,"message":"no such table: raw_mails: SQLITE_ERROR"}]}'
    )

    def fake_urlopen(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise HTTPError(
            url="https://api.cloudflare.com/client/v4/accounts/acct-123/d1/database/db-1/query",
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=FakeHttpResponse(payload),
        )

    monkeypatch.setattr(d1_cleanup, "urlopen", fake_urlopen)

    try:
        d1_cleanup._query("db-1", "SELECT COUNT(*) AS count FROM raw_mails")
    except d1_cleanup.D1TableMissingError as exc:
        assert "no such table: raw_mails" in str(exc)
    else:
        raise AssertionError("expected D1TableMissingError")

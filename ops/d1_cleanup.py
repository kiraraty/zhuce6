"""Periodic Cloudflare D1 cleanup for cfmail storage tables."""

from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .common import now

DEFAULT_D1_DATABASE_ID = ""
DEFAULT_D1_MAIL_RETENTION_HOURS = 2
DEFAULT_D1_ADDRESS_RETENTION_HOURS = 24
DEFAULT_D1_CLEANUP_BATCH_SIZE = 5000
_QUERY_TIMEOUT_SECONDS = 30
_CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"

_missing_credentials_warned = False


class D1CleanupError(RuntimeError):
    """Raised when the D1 query API returns a non-recoverable error."""


class D1TableMissingError(D1CleanupError):
    """Raised when the target D1 table does not exist."""


def _warning(message: str) -> None:
    print(f"[{now()}] [d1_cleanup] warning: {message}")


def _credentials_from_env() -> tuple[str, str, str] | None:
    global _missing_credentials_warned

    auth_email = str(os.getenv("ZHUCE6_CFMAIL_CF_AUTH_EMAIL", "")).strip()
    auth_key = str(os.getenv("ZHUCE6_CFMAIL_CF_AUTH_KEY", "")).strip()
    account_id = str(os.getenv("ZHUCE6_CFMAIL_CF_ACCOUNT_ID", "")).strip()
    if auth_email and auth_key and account_id:
        _missing_credentials_warned = False
        return auth_email, auth_key, account_id
    if not _missing_credentials_warned:
        _warning(
            "missing Cloudflare credentials, skip cleanup "
            "(need ZHUCE6_CFMAIL_CF_AUTH_EMAIL / ZHUCE6_CFMAIL_CF_AUTH_KEY / ZHUCE6_CFMAIL_CF_ACCOUNT_ID)"
        )
        _missing_credentials_warned = True
    return None


def _error_messages(payload: dict[str, Any]) -> list[str]:
    messages: list[str] = []
    for bucket in ("errors", "messages"):
        items = payload.get(bucket)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                message = str(item.get("message") or "").strip()
                if message:
                    messages.append(message)
    result_items = payload.get("result")
    if isinstance(result_items, list):
        for result in result_items:
            if not isinstance(result, dict):
                continue
            if bool(result.get("success", True)):
                continue
            message = str(result.get("error") or result.get("message") or "").strip()
            if message:
                messages.append(message)
    return messages


def _raise_for_payload(payload: dict[str, Any]) -> None:
    messages = _error_messages(payload)
    text = " | ".join(messages) if messages else json.dumps(payload, ensure_ascii=False)
    lowered = text.lower()
    if "no such table" in lowered or "sqlite_error" in lowered:
        raise D1TableMissingError(text)
    raise D1CleanupError(text)


def _query(database_id: str, sql: str, params: list[Any] | None = None) -> dict[str, Any]:
    credentials = _credentials_from_env()
    if credentials is None:
        raise D1CleanupError("missing_cloudflare_credentials")
    auth_email, auth_key, account_id = credentials
    url = f"{_CLOUDFLARE_API_BASE}/accounts/{account_id}/d1/database/{database_id}/query"
    body = {"sql": sql}
    if params:
        body["params"] = params
    request = Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Auth-Email": auth_email,
            "X-Auth-Key": auth_key,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=_QUERY_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(exc)
        try:
            payload = json.loads(detail)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            _raise_for_payload(payload)
        raise D1CleanupError(f"http {exc.code}: {detail}") from exc
    except URLError as exc:
        raise D1CleanupError(f"network error: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise D1CleanupError(f"invalid json response: {exc}") from exc

    if not bool(payload.get("success", False)):
        _raise_for_payload(payload)
    return payload


def _first_result(payload: dict[str, Any]) -> dict[str, Any]:
    results = payload.get("result")
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        raise D1CleanupError("missing result payload")
    result = results[0]
    if not bool(result.get("success", True)):
        _raise_for_payload(payload)
    return result


def _query_once(database_id: str, sql: str, params: list[Any] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = _query(database_id, sql, params=params)
    result = _first_result(payload)
    rows = result.get("results")
    if not isinstance(rows, list):
        rows = []
    meta = result.get("meta")
    if not isinstance(meta, dict):
        meta = {}
    normalized_rows = [row for row in rows if isinstance(row, dict)]
    return normalized_rows, meta


def _count_rows(database_id: str, table: str) -> tuple[int, int | None]:
    rows, meta = _query_once(database_id, f"SELECT COUNT(*) AS count FROM {table}")
    count = 0
    if rows:
        try:
            count = int(rows[0].get("count") or 0)
        except Exception:
            count = 0
    size_after = meta.get("size_after")
    try:
        return count, int(size_after) if size_after is not None else None
    except Exception:
        return count, None


def _delete_in_batches(database_id: str, table: str, retention_hours: int, batch_size: int) -> tuple[int, int | None]:
    total_deleted = 0
    latest_size_after: int | None = None
    safe_retention = max(0, int(retention_hours))
    safe_batch_size = max(1, int(batch_size))
    sql = (
        f"DELETE FROM {table} "
        f"WHERE created_at < datetime('now', '-{safe_retention} hours') "
        f"LIMIT {safe_batch_size}"
    )
    while True:
        _rows, meta = _query_once(database_id, sql)
        changes_raw = meta.get("changes")
        try:
            changes = int(changes_raw or 0)
        except Exception:
            changes = 0
        size_after = meta.get("size_after")
        try:
            latest_size_after = int(size_after) if size_after is not None else latest_size_after
        except Exception:
            pass
        if changes <= 0:
            break
        total_deleted += changes
    return total_deleted, latest_size_after


def _final_size_after(database_id: str) -> int | None:
    _rows, meta = _query_once(database_id, "SELECT 1 AS ok")
    size_after = meta.get("size_after")
    try:
        return int(size_after) if size_after is not None else None
    except Exception:
        return None


def d1_cleanup_once(
    database_id: str = DEFAULT_D1_DATABASE_ID,
    mail_retention_hours: int = DEFAULT_D1_MAIL_RETENTION_HOURS,
    address_retention_hours: int = DEFAULT_D1_ADDRESS_RETENTION_HOURS,
    batch_size: int = DEFAULT_D1_CLEANUP_BATCH_SIZE,
) -> dict[str, object]:
    summary: dict[str, object] = {
        "deleted_mails": 0,
        "deleted_addresses": 0,
        "deleted_senders": 0,
        "size_after_bytes": None,
        "skipped_reason": None,
    }
    normalized_database_id = str(database_id or "").strip()
    if not normalized_database_id:
        summary["skipped_reason"] = "missing_database_id"
        return summary

    if _credentials_from_env() is None:
        summary["skipped_reason"] = "missing_cloudflare_credentials"
        return summary

    size_after_bytes: int | None = None
    try:
        raw_mails_count, size_after_bytes = _count_rows(normalized_database_id, "raw_mails")
    except D1TableMissingError:
        _warning("table raw_mails not found, skip count")
        raw_mails_count = 0
    try:
        address_count, count_size_after = _count_rows(normalized_database_id, "address")
        if count_size_after is not None:
            size_after_bytes = count_size_after
    except D1TableMissingError:
        _warning("table address not found, skip count")
        address_count = 0

    if raw_mails_count == 0 and address_count == 0:
        summary["size_after_bytes"] = size_after_bytes
        summary["skipped_reason"] = "nothing_to_clean"
        print(f"[{now()}] [d1_cleanup] nothing to clean")
        return summary

    try:
        deleted_mails, delete_size_after = _delete_in_batches(
            normalized_database_id,
            "raw_mails",
            retention_hours=mail_retention_hours,
            batch_size=batch_size,
        )
        summary["deleted_mails"] = deleted_mails
        if delete_size_after is not None:
            size_after_bytes = delete_size_after
    except D1TableMissingError:
        _warning("table raw_mails not found, skip cleanup")

    try:
        deleted_addresses, delete_size_after = _delete_in_batches(
            normalized_database_id,
            "address",
            retention_hours=address_retention_hours,
            batch_size=batch_size,
        )
        summary["deleted_addresses"] = deleted_addresses
        if delete_size_after is not None:
            size_after_bytes = delete_size_after
    except D1TableMissingError:
        _warning("table address not found, skip cleanup")

    try:
        deleted_senders, delete_size_after = _delete_in_batches(
            normalized_database_id,
            "address_sender",
            retention_hours=address_retention_hours,
            batch_size=batch_size,
        )
        summary["deleted_senders"] = deleted_senders
        if delete_size_after is not None:
            size_after_bytes = delete_size_after
    except D1TableMissingError:
        _warning("table address_sender not found, skip cleanup")

    try:
        final_size_after = _final_size_after(normalized_database_id)
        if final_size_after is not None:
            size_after_bytes = final_size_after
    except D1CleanupError as exc:
        _warning(f"final size check failed: {exc}")

    summary["size_after_bytes"] = size_after_bytes
    size_mb_text = "unknown"
    if isinstance(size_after_bytes, int):
        size_mb_text = f"{size_after_bytes / (1024 * 1024):.1f}MB"
    print(
        f"[{now()}] [d1_cleanup] 清理完成 | raw_mails=-{summary['deleted_mails']} "
        f"| address=-{summary['deleted_addresses']} | address_sender=-{summary['deleted_senders']} "
        f"| size={size_mb_text}"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="清理 cfmail Cloudflare D1 历史数据")
    parser.add_argument("--once", action="store_true", help="只执行一轮")
    parser.add_argument("--interval", type=int, default=1800, help="清理间隔秒数")
    parser.add_argument("--database-id", default=DEFAULT_D1_DATABASE_ID, help="Cloudflare D1 database id")
    parser.add_argument(
        "--mail-retention-hours",
        type=int,
        default=DEFAULT_D1_MAIL_RETENTION_HOURS,
        help="raw_mails 保留小时数",
    )
    parser.add_argument(
        "--address-retention-hours",
        type=int,
        default=DEFAULT_D1_ADDRESS_RETENTION_HOURS,
        help="address / address_sender 保留小时数",
    )
    args = parser.parse_args()

    interval = max(1, int(args.interval))
    while True:
        started_at = time.time()
        try:
            d1_cleanup_once(
                database_id=str(args.database_id).strip(),
                mail_retention_hours=int(args.mail_retention_hours),
                address_retention_hours=int(args.address_retention_hours),
            )
        except Exception as exc:
            _warning(f"cleanup failed: {exc}")
        elapsed = time.time() - started_at
        if args.once:
            break
        time.sleep(max(0, interval - elapsed))


if __name__ == "__main__":
    main()

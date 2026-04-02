"""Quota probing helpers for rotate."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import re

from .common import cpa_management_request

P401 = re.compile(
    r"(^|\D)401(\D|$)|unauthorized|unauthenticated|"
    r"token\s+expired|authentication\s+token\s+is\s+expired|login\s+required|"
    r"authentication\s+failed|token.+invalidated|token_invalidated",
    re.I,
)
P429 = re.compile(r"(^|\D)429(\D|$)|usage_limit_reached|rate_limit_exceeded", re.I)
P_DEACTIVATED = re.compile(r"account[_\s-]*deactivated|has been deactivated", re.I)
QUOTA_VALIDATE_URL = "https://chatgpt.com/backend-api/wham/usage"
QUOTA_VALIDATE_USER_AGENT = "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal"


def _compact_text(value: str, limit: int = 240) -> str:
    return " ".join(str(value or "").split())[:limit]


def classify_status_message(status_message: str) -> int:
    raw = str(status_message or "").strip()
    if not raw:
        return 200
    if P401.search(raw) or P_DEACTIVATED.search(raw):
        return 401
    if P429.search(raw):
        return 429
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0
    err = payload.get("error")
    if not isinstance(err, dict):
        return 0
    err_type = str(err.get("type") or "").strip().lower()
    err_code = str(err.get("code") or "").strip().lower()
    err_message = str(err.get("message") or "").strip()
    if (
        err_type in {"unauthorized", "invalidated", "account_deactivated"}
        or err_code in {"token_invalidated", "account_deactivated"}
        or P401.search(err_message)
        or P_DEACTIVATED.search(err_message)
    ):
        return 401
    if err_type in {"usage_limit_reached", "rate_limit_exceeded"} or P429.search(err_message):
        return 429
    return 0


def is_deactivated_status_message(status_message: str) -> bool:
    raw = str(status_message or "").strip()
    if not raw:
        return False
    if P_DEACTIVATED.search(raw):
        return True
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    err = payload.get("error")
    if not isinstance(err, dict):
        return False
    err_type = str(err.get("type") or "").strip().lower()
    err_code = str(err.get("code") or "").strip().lower()
    err_message = str(err.get("message") or "").strip()
    return err_type == "account_deactivated" or err_code == "account_deactivated" or bool(P_DEACTIVATED.search(err_message))


def _extract_entry_account_id(entry: dict) -> str:
    id_token = entry.get("id_token")
    if isinstance(id_token, dict):
        account_id = str(id_token.get("chatgpt_account_id") or id_token.get("account_id") or "").strip()
        if account_id:
            return account_id
    return str(entry.get("account_id") or "").strip()


def _extract_header_value(headers: object, key: str) -> str:
    if not isinstance(headers, dict):
        return ""
    for header_key, header_value in headers.items():
        if str(header_key or "").strip().lower() != key.strip().lower():
            continue
        if isinstance(header_value, list):
            for item in header_value:
                value = str(item or "").strip()
                if value:
                    return value
            return ""
        return str(header_value or "").strip()
    return ""


def _can_probe_quota(entry: dict) -> bool:
    provider = str(entry.get("provider") or "").strip().lower()
    if provider and provider != "codex":
        return False
    auth_index = str(entry.get("auth_index") or "").strip()
    account_id = _extract_entry_account_id(entry)
    return bool(auth_index and account_id)


def _probe_quota_status(entry: dict, key: str, management_base_url: str) -> tuple[int, str, bool]:
    auth_index = str(entry.get("auth_index") or "").strip()
    account_id = _extract_entry_account_id(entry)
    if not auth_index or not account_id:
        return 0, "missing auth_index or account_id", False

    body = json.dumps(
        {
            "authIndex": auth_index,
            "method": "GET",
            "url": QUOTA_VALIDATE_URL,
            "header": {
                "Authorization": "Bearer $TOKEN$",
                "Content-Type": "application/json",
                "User-Agent": QUOTA_VALIDATE_USER_AGENT,
                "Chatgpt-Account-Id": account_id,
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    status, payload = cpa_management_request(
        "POST",
        "api-call",
        key,
        management_base_url=management_base_url,
        body=body,
        content_type="application/json",
        timeout=60,
    )
    if status == 0 or not isinstance(payload, dict):
        return 0, "quota probe unavailable", False

    probe_status_code = payload.get("status_code") or payload.get("statusCode") or 0
    try:
        probe_status_code = int(probe_status_code)
    except Exception:
        probe_status_code = 0

    headers = payload.get("header") or payload.get("headers") or {}
    raw_body = payload.get("body")
    if isinstance(raw_body, str):
        body_text = raw_body
    elif raw_body is None:
        body_text = ""
    else:
        try:
            body_text = json.dumps(raw_body, ensure_ascii=False)
        except Exception:
            body_text = str(raw_body)

    header_auth_error = _extract_header_value(headers, "X-Openai-Authorization-Error")
    header_error_code = _extract_header_value(headers, "X-Openai-Ide-Error-Code")
    deactivated = is_deactivated_status_message(body_text) or header_error_code == "account_deactivated"
    body_code = classify_status_message(body_text)

    if (
        probe_status_code == 401
        or header_auth_error == "401"
        or header_error_code in {"token_invalidated", "account_deactivated"}
        or body_code == 401
    ):
        detail = body_text or header_error_code or header_auth_error or "quota probe returned 401"
        return 401, _compact_text(detail), deactivated

    if probe_status_code == 429 or body_code == 429:
        detail = body_text or "quota probe returned 429"
        return 429, _compact_text(detail), False

    if probe_status_code == 200:
        return 200, _compact_text(body_text or "active"), False

    return 0, _compact_text(body_text or f"quota probe status={probe_status_code}"), deactivated


def _collect_quota_probe_results(
    entries: list[dict],
    *,
    management_key: str,
    management_base_url: str,
    max_count: int,
    workers: int,
) -> tuple[dict[str, tuple[int, str, bool]], dict[str, int]]:
    probe_candidates = [entry for entry in entries if _can_probe_quota(entry)]
    skipped = 0
    if max_count > 0 and len(probe_candidates) > max_count:
        skipped = len(probe_candidates) - max_count
        probe_candidates = probe_candidates[:max_count]

    results: dict[str, tuple[int, str, bool]] = {}
    if not probe_candidates:
        return results, {"probed": 0, "probe_401": 0, "probe_429": 0, "probe_skipped": skipped}

    max_workers = max(1, min(int(workers), len(probe_candidates)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(_probe_quota_status, entry, management_key, management_base_url): str(entry.get("name", ""))
            for entry in probe_candidates
        }
        for future in as_completed(future_map):
            name = future_map[future]
            try:
                results[name] = future.result()
            except Exception as exc:
                results[name] = (0, _compact_text(str(exc) or "quota probe failed"), False)

    counters = {
        "probed": len(results),
        "probe_401": sum(1 for code, _detail, _deactivated in results.values() if code == 401),
        "probe_429": sum(1 for code, _detail, _deactivated in results.values() if code == 429),
        "probe_skipped": skipped,
    }
    return results, counters


def _needs_service_probe(entry: dict) -> bool:
    provider = str(entry.get("provider") or "").strip().lower()
    if provider and provider != "codex":
        return False
    status = str(entry.get("status") or "").strip().lower()
    if status != "error":
        return False
    status_message = str(entry.get("status_message") or "").strip()
    return classify_status_message(status_message) == 0

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any, TextIO

from curl_cffi import requests as cffi_requests

from core.cfmail import load_cfmail_accounts_from_file
from core.env_loader import load_env_file
from core.paths import DEFAULT_ENV_FILE, resolve_cfmail_config_path


def _print(stdout: TextIO, message: str) -> None:
    print(message, file=stdout)


def _normalize_domain(value: str) -> str:
    return str(value or "").strip().lower().rstrip(".")


def _load_active_domain(config_path: Path) -> str:
    accounts = [
        item
        for item in load_cfmail_accounts_from_file(config_path, silent=False)
        if isinstance(item, dict) and item.get("enabled", True)
    ]
    for item in reversed(accounts):
        domain = _normalize_domain(str(item.get("email_domain") or ""))
        if domain:
            return domain
    raise RuntimeError(f"no active cfmail domain found in {config_path}")


def _load_env(env_file: Path) -> None:
    load_env_file(env_file)
    cfmail_env_file = Path(
        str(os.getenv("ZHUCE6_CFMAIL_ENV_FILE", env_file.parent / "config" / "cfmail_provision.env")).strip()
        or str(env_file.parent / "config" / "cfmail_provision.env")
    ).expanduser().resolve()
    load_env_file(cfmail_env_file)


def _headers(auth_email: str, auth_key: str) -> dict[str, str]:
    return {
        "X-Auth-Email": auth_email,
        "X-Auth-Key": auth_key,
        "Content-Type": "application/json",
    }


def _request(method: str, url: str, *, headers: dict[str, str]) -> dict[str, Any]:
    response = cffi_requests.request(
        method.upper(),
        url,
        headers=headers,
        timeout=30,
        impersonate="chrome",
    )
    payload = response.json() if response.content else {}
    if response.status_code >= 400 or not payload.get("success", False):
        raise RuntimeError(f"{method.upper()} {url} failed: HTTP {response.status_code} {json.dumps(payload, ensure_ascii=False)}")
    return payload


def _request_paginated(url: str, *, headers: dict[str, str]) -> list[dict[str, Any]]:
    page = 1
    results: list[dict[str, Any]] = []
    while True:
        separator = "&" if "?" in url else "?"
        payload = _request("GET", f"{url}{separator}page={page}&per_page=100", headers=headers)
        items = payload.get("result") or []
        if isinstance(items, list):
            results.extend(item for item in items if isinstance(item, dict))
        info = payload.get("result_info") or {}
        total_pages = int(info.get("total_pages") or 1)
        if page >= total_pages:
            break
        page += 1
    return results


def _routing_rule_domains(rule: dict[str, Any]) -> set[str]:
    domains: set[str] = set()
    for matcher in rule.get("matchers") or []:
        if not isinstance(matcher, dict):
            continue
        value = _normalize_domain(str(matcher.get("value") or ""))
        if "*@" in value:
            domains.add(value.split("*@", 1)[-1])
    return domains


def run_cleanup(
    *,
    env_file: Path | None = None,
    config_path: Path | None = None,
    stdout: TextIO | None = None,
) -> dict[str, Any]:
    out = stdout or sys.stdout
    resolved_env_file = (env_file or DEFAULT_ENV_FILE).expanduser().resolve()
    resolved_config_path = (config_path or resolve_cfmail_config_path()).expanduser().resolve()
    _load_env(resolved_env_file)

    auth_email = str(os.getenv("ZHUCE6_CFMAIL_CF_AUTH_EMAIL", "")).strip()
    auth_key = str(os.getenv("ZHUCE6_CFMAIL_CF_AUTH_KEY", "")).strip()
    zone_id = str(os.getenv("ZHUCE6_CFMAIL_CF_ZONE_ID", "")).strip()
    zone_name = _normalize_domain(str(os.getenv("ZHUCE6_CFMAIL_ZONE_NAME", "")))
    missing = [
        name
        for name, value in (
            ("ZHUCE6_CFMAIL_CF_AUTH_EMAIL", auth_email),
            ("ZHUCE6_CFMAIL_CF_AUTH_KEY", auth_key),
            ("ZHUCE6_CFMAIL_CF_ZONE_ID", zone_id),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"missing cleanup env: {', '.join(missing)}")

    active_domain = _load_active_domain(resolved_config_path)
    headers = _headers(auth_email, auth_key)
    base_url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}"
    zone_suffix = f".{zone_name}" if zone_name else ""

    _print(out, f"[cleanup] env: {resolved_env_file}")
    _print(out, f"[cleanup] config: {resolved_config_path}")
    _print(out, f"[cleanup] active domain: {active_domain}")

    rules = _request_paginated(f"{base_url}/email/routing/rules", headers=headers)
    _print(out, f"[cleanup] routing rules fetched: {len(rules)}")
    removed_routing_rules: list[str] = []
    for rule in rules:
        rule_id = str(rule.get("id") or "").strip()
        rule_name = str(rule.get("name") or "").strip()
        domains = _routing_rule_domains(rule)
        should_keep = active_domain in domains or "nova" in rule_name.lower()
        if should_keep or not domains:
            continue
        _print(out, f"[cleanup] delete routing rule: {rule_id} name={rule_name}")
        try:
            _request("DELETE", f"{base_url}/email/routing/rules/{rule_id}", headers=headers)
            removed_routing_rules.append(rule_id)
        except Exception as exc:
            _print(out, f"[cleanup]   skip routing rule {rule_id}: {exc}")

    dns_records = _request_paginated(f"{base_url}/dns_records", headers=headers)
    _print(out, f"[cleanup] dns records fetched: {len(dns_records)}")
    removed_dns_records: list[str] = []
    for record in dns_records:
        record_id = str(record.get("id") or "").strip()
        record_type = str(record.get("type") or "").strip().upper()
        record_name = _normalize_domain(str(record.get("name") or ""))
        if record_type not in {"MX", "TXT"}:
            continue
        if not record_name.startswith("auto"):
            continue
        if zone_suffix and not record_name.endswith(zone_suffix):
            continue
        if record_name == active_domain:
            continue
        _print(out, f"[cleanup] delete dns record: {record_id} type={record_type} name={record_name}")
        try:
            _request("DELETE", f"{base_url}/dns_records/{record_id}", headers=headers)
            removed_dns_records.append(record_id)
        except Exception as exc:
            _print(out, f"[cleanup]   skip dns record {record_id}: {exc}")

    summary = {
        "active_domain": active_domain,
        "removed_routing_rules": removed_routing_rules,
        "removed_dns_records": removed_dns_records,
    }
    _print(out, f"[cleanup] summary: {json.dumps(summary, ensure_ascii=False)}")
    return summary


def main() -> int:
    run_cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

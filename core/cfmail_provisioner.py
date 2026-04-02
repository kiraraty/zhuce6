"""Cloudflare-backed cfmail subdomain provisioner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
from typing import Any

from curl_cffi import requests as cffi_requests

from .cfmail import DEFAULT_CFMAIL_CONFIG_PATH, load_cfmail_accounts_from_file

MX_RECORDS = (
    ("route1.mx.cloudflare.net", 20),
    ("route2.mx.cloudflare.net", 85),
    ("route3.mx.cloudflare.net", 36),
)


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%m%d%H%M%S")


def _normalize_host(value: str) -> str:
    candidate = str(value or "").strip()
    if candidate.startswith("https://"):
        candidate = candidate[len("https://") :]
    elif candidate.startswith("http://"):
        candidate = candidate[len("http://") :]
    return candidate.strip().strip("/")


@dataclass(frozen=True)
class ProvisioningSettings:
    auth_email: str
    auth_key: str
    api_token: str
    account_id: str
    zone_id: str
    worker_name: str
    zone_name: str

    @classmethod
    def from_env(cls) -> "ProvisioningSettings":
        return cls(
            auth_email=str(os.getenv("ZHUCE6_CFMAIL_CF_AUTH_EMAIL", "")).strip(),
            auth_key=str(os.getenv("ZHUCE6_CFMAIL_CF_AUTH_KEY", "")).strip(),
            api_token=str(os.getenv("ZHUCE6_CFMAIL_API_TOKEN", "")).strip(),
            account_id=str(os.getenv("ZHUCE6_CFMAIL_CF_ACCOUNT_ID", "")).strip(),
            zone_id=str(os.getenv("ZHUCE6_CFMAIL_CF_ZONE_ID", "")).strip(),
            worker_name=str(os.getenv("ZHUCE6_CFMAIL_WORKER_NAME", "")).strip(),
            zone_name=str(os.getenv("ZHUCE6_CFMAIL_ZONE_NAME", "")).strip(),
        )

    @property
    def uses_api_token(self) -> bool:
        return bool(self.api_token) and not (self.auth_email and self.auth_key)

    def validate(self) -> None:
        has_token = bool(self.api_token)
        has_key = bool(self.auth_email and self.auth_key)
        if not has_token and not has_key:
            missing = [
                name
                for name, value in (
                    ("ZHUCE6_CFMAIL_CF_AUTH_EMAIL", self.auth_email),
                    ("ZHUCE6_CFMAIL_CF_AUTH_KEY", self.auth_key),
                )
                if not value
            ]
            raise RuntimeError(f"missing cfmail provisioning env: {', '.join(missing)}")
        missing = [
            name
            for name, value in (
                ("ZHUCE6_CFMAIL_CF_ACCOUNT_ID", self.account_id),
                ("ZHUCE6_CFMAIL_CF_ZONE_ID", self.zone_id),
                ("ZHUCE6_CFMAIL_WORKER_NAME", self.worker_name),
                ("ZHUCE6_CFMAIL_ZONE_NAME", self.zone_name),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"missing cfmail provisioning env: {', '.join(missing)}")


@dataclass(frozen=True)
class ProvisionResult:
    success: bool
    step: str
    old_domain: str = ""
    new_domain: str = ""
    error: str = ""


class CfmailProvisioner:
    def __init__(
        self,
        *,
        config_path: str | Path | None = None,
        proxy_url: str | None = None,
        settings: ProvisioningSettings | None = None,
    ) -> None:
        self.config_path = Path(config_path or DEFAULT_CFMAIL_CONFIG_PATH)
        self.settings = settings or ProvisioningSettings.from_env()
        self.proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

    def _headers(self) -> dict[str, str]:
        self.settings.validate()
        if self.settings.uses_api_token:
            return {"Authorization": f"Bearer {self.settings.api_token}"}
        return {
            "X-Auth-Email": self.settings.auth_email,
            "X-Auth-Key": self.settings.auth_key,
        }

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = cffi_requests.request(
            method.upper(),
            url,
            headers={
                **self._headers(),
                "Content-Type": "application/json",
            },
            json=json_body,
            proxies=self.proxies,
            timeout=30,
            impersonate="chrome",
        )
        data = response.json() if response.content else {}
        if response.status_code >= 400 or not data.get("success", False):
            raise RuntimeError(f"{method.upper()} {url} failed: HTTP {response.status_code} {data}")
        return data

    def _request_paginated(self, url: str) -> list[dict[str, Any]]:
        page = 1
        results: list[dict[str, Any]] = []
        while True:
            separator = "&" if "?" in url else "?"
            payload = self._request("GET", f"{url}{separator}page={page}&per_page=100")
            items = payload.get("result") or []
            if isinstance(items, list):
                results.extend(item for item in items if isinstance(item, dict))
            info = payload.get("result_info") or {}
            total_pages = int(info.get("total_pages") or 1)
            if page >= total_pages:
                break
            page += 1
        return results

    def _get_worker_settings(self) -> dict[str, Any]:
        url = (
            f"https://api.cloudflare.com/client/v4/accounts/{self.settings.account_id}/workers/scripts/"
            f"{self.settings.worker_name}/settings"
        )
        return self._request("GET", url).get("result") or {}

    def _patch_worker_settings(self, bindings: list[dict[str, Any]]) -> None:
        url = (
            f"https://api.cloudflare.com/client/v4/accounts/{self.settings.account_id}/workers/scripts/"
            f"{self.settings.worker_name}/settings"
        )
        self.settings.validate()
        body = json.dumps({"bindings": bindings}, ensure_ascii=False, separators=(",", ":"))
        boundary = secrets.token_hex(16)
        multipart_body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="settings"\r\n'
            f"Content-Type: application/json\r\n"
            f"\r\n"
            f"{body}\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")
        last_error = ""
        for attempt in range(3):
            try:
                response = cffi_requests.patch(
                    url,
                    headers={
                        **self._headers(),
                        "Content-Type": f"multipart/form-data; boundary={boundary}",
                    },
                    data=multipart_body,
                    proxies=self.proxies,
                    timeout=30,
                    impersonate="chrome",
                )
                data = response.json() if response.content else {}
                if response.status_code >= 400 or not data.get("success", False):
                    last_error = f"HTTP {response.status_code} {data}"
                    if attempt < 2:
                        time.sleep(2)
                        continue
                    raise RuntimeError(f"PATCH worker settings failed: {last_error}")
                return
            except RuntimeError:
                raise
            except Exception as exc:
                last_error = str(exc)
                if attempt < 2:
                    time.sleep(2)
                    continue
                raise RuntimeError(f"PATCH worker settings failed after {attempt + 1} attempts: {last_error}")

    def _make_new_label(self) -> str:
        return f"auto{_utc_stamp()}{secrets.token_hex(2)}"

    def _new_domain(self, label: str) -> str:
        return f"{label}.{self.settings.zone_name}"

    def _create_email_routing_rule(self, domain: str, label: str) -> None:
        url = f"https://api.cloudflare.com/client/v4/zones/{self.settings.zone_id}/email/routing/rules"
        self._request(
            "POST",
            url,
            json_body={
                "name": f"{label} subdomain catch-all",
                "enabled": True,
                "matchers": [{"type": "literal", "field": "to", "value": f"*@" + domain}],
                "actions": [{"type": "worker", "value": [self.settings.worker_name]}],
            },
        )

    def _create_dns_records(self, domain: str) -> None:
        url = f"https://api.cloudflare.com/client/v4/zones/{self.settings.zone_id}/dns_records"
        for content, priority in MX_RECORDS:
            self._request(
                "POST",
                url,
                json_body={
                    "type": "MX",
                    "name": domain,
                    "content": content,
                    "priority": priority,
                    "ttl": 1,
                },
            )
        self._request(
            "POST",
            url,
            json_body={
                "type": "TXT",
                "name": domain,
                "content": "v=spf1 include:_spf.mx.cloudflare.net ~all",
                "ttl": 1,
            },
        )

    def _list_dns_records(self) -> list[dict[str, Any]]:
        url = f"https://api.cloudflare.com/client/v4/zones/{self.settings.zone_id}/dns_records"
        return self._request_paginated(url)

    def _delete_dns_record(self, record_id: str) -> None:
        url = f"https://api.cloudflare.com/client/v4/zones/{self.settings.zone_id}/dns_records/{record_id}"
        self._request("DELETE", url)

    def _list_email_routing_rules(self) -> list[dict[str, Any]]:
        url = f"https://api.cloudflare.com/client/v4/zones/{self.settings.zone_id}/email/routing/rules"
        return self._request_paginated(url)

    def _delete_email_routing_rule(self, rule_id: str) -> None:
        url = f"https://api.cloudflare.com/client/v4/zones/{self.settings.zone_id}/email/routing/rules/{rule_id}"
        self._request("DELETE", url)

    def _routing_rule_domains(self, rule: dict[str, Any]) -> set[str]:
        domains: set[str] = set()
        for matcher in rule.get("matchers") or []:
            if not isinstance(matcher, dict):
                continue
            value = str(matcher.get("value") or "").strip().lower()
            if "*@" in value:
                domains.add(value.split("*@", 1)[-1])
        return domains

    def _normalize_domain_name(self, value: str) -> str:
        return str(value or "").strip().lower().rstrip(".")

    def _is_managed_auto_domain(self, domain: str) -> bool:
        domain_key = self._normalize_domain_name(domain)
        zone_suffix = f".{self.settings.zone_name.lower()}"
        return bool(domain_key) and domain_key.startswith("auto") and domain_key.endswith(zone_suffix)

    def _delete_domain_artifacts(self, domain: str) -> None:
        domain_key = str(domain or "").strip().lower()
        if not domain_key:
            return
        for record in self._list_dns_records():
            if str(record.get("name") or "").strip().lower() == domain_key:
                record_id = str(record.get("id") or "").strip()
                if record_id:
                    try:
                        self._delete_dns_record(record_id)
                    except Exception:
                        pass  # skip read-only or protected records
        for rule in self._list_email_routing_rules():
            if domain_key in self._routing_rule_domains(rule):
                rule_id = str(rule.get("id") or "").strip()
                if rule_id:
                    try:
                        self._delete_email_routing_rule(rule_id)
                    except Exception:
                        pass

    def _managed_auto_domains(self, accounts: list[dict[str, Any]]) -> list[str]:
        return [
            self._normalize_domain_name(str(item.get("email_domain") or ""))
            for item in accounts
            if self._is_managed_auto_domain(str(item.get("email_domain") or ""))
        ]

    def cleanup_stale_domains(self, keep_domains: set[str] | list[str] | None = None) -> dict[str, Any]:
        return self.cleanup_stale_cf_resources(keep_domains=keep_domains)

    def cleanup_stale_cf_resources(self, keep_domains: set[str] | list[str] | None = None) -> dict[str, Any]:
        accounts = self._load_all_accounts()
        active_domain = self._normalize_domain_name(str(self.current_active_account().get("email_domain") or ""))
        keep_set = {
            self._normalize_domain_name(domain)
            for domain in (keep_domains or [])
            if self._normalize_domain_name(domain)
        }
        keep_set.discard(active_domain)
        stale_domains: set[str] = set()
        removed_dns_records: list[str] = []
        removed_routing_rules: list[str] = []
        errors: list[str] = []
        for rule in self._list_email_routing_rules():
            rule_domains = {
                domain
                for domain in self._routing_rule_domains(rule)
                if self._is_managed_auto_domain(domain) and domain != active_domain and domain not in keep_set
            }
            stale_domains.update(rule_domains)
            if not rule_domains:
                continue
            rule_id = str(rule.get("id") or "").strip()
            if not rule_id:
                continue
            try:
                self._delete_email_routing_rule(rule_id)
                removed_routing_rules.append(rule_id)
            except Exception as exc:
                errors.append(f"routing_rule:{rule_id}: {exc}")
        for record in self._list_dns_records():
            record_type = str(record.get("type") or "").strip().upper()
            if record_type not in {"MX", "TXT"}:
                continue
            domain = self._normalize_domain_name(str(record.get("name") or ""))
            if not self._is_managed_auto_domain(domain) or domain == active_domain or domain in keep_set:
                continue
            stale_domains.add(domain)
            record_id = str(record.get("id") or "").strip()
            if not record_id:
                continue
            try:
                self._delete_dns_record(record_id)
                removed_dns_records.append(record_id)
            except Exception as exc:
                errors.append(f"dns_record:{record_id}: {exc}")
        stale_account_domains = set(self._managed_auto_domains(accounts)).intersection(stale_domains)
        if stale_account_domains:
            pruned_accounts = [
                item for item in accounts
                if self._normalize_domain_name(str(item.get("email_domain") or "")) not in stale_account_domains
            ]
            if len(pruned_accounts) != len(accounts):
                self._write_accounts(pruned_accounts)
        return {
            "removed_domains": sorted(stale_domains),
            "removed_dns_records": removed_dns_records,
            "removed_routing_rules": removed_routing_rules,
            "errors": errors,
        }

    def _is_record_quota_error(self, exc: Exception) -> bool:
        message = str(exc or "")
        return "81045" in message or "Record quota exceeded" in message

    def _update_worker_domains(self, domain: str, old_domain: str | None = None) -> None:
        settings = self._get_worker_settings()
        bindings = list(settings.get("bindings") or [])
        domains = [domain]
        old_domain_key = self._normalize_domain_name(old_domain or "")
        if old_domain_key and old_domain_key != self._normalize_domain_name(domain):
            domains.append(old_domain_key)
        updated = False
        for binding in bindings:
            if binding.get("name") not in {"DOMAINS", "DEFAULT_DOMAINS"} or binding.get("type") != "json":
                continue
            binding["json"] = domains
            updated = True
        if not updated:
            raise RuntimeError("worker DOMAINS bindings missing")
        self._patch_worker_settings(bindings)

    def smoke_test(self, worker_domain: str, admin_password: str, email_domain: str) -> None:
        test_name = f"smoke{secrets.token_hex(3)}"
        required_successes = 3
        success_streak = 0
        last_error = "smoke test did not run"
        for attempt in range(1, 10):
            response = cffi_requests.post(
                f"https://{_normalize_host(worker_domain)}/admin/new_address",
                headers={
                    "x-admin-auth": admin_password,
                    "Content-Type": "application/json",
                },
                json={"enablePrefix": True, "name": f"{test_name}{attempt}", "domain": email_domain},
                proxies=self.proxies,
                timeout=20,
                impersonate="chrome",
            )
            if response.status_code != 200:
                success_streak = 0
                last_error = f"HTTP {response.status_code} {response.text[:240]}"
                time.sleep(min(8, attempt * 2))
                continue
            try:
                data = response.json() if response.content else {}
            except Exception:
                success_streak = 0
                last_error = f"non-json response: {response.text[:240]}"
                time.sleep(min(8, attempt * 2))
                continue
            if str(data.get("address") or "").strip() and str(data.get("jwt") or "").strip():
                success_streak += 1
                if success_streak >= required_successes:
                    return
                last_error = f"smoke success streak={success_streak}/{required_successes}"
                time.sleep(1)
                continue
            success_streak = 0
            last_error = f"incomplete payload: {json.dumps(data, ensure_ascii=False)[:240]}"
            time.sleep(min(8, attempt * 2))
        raise RuntimeError(f"smoke test failed: {last_error}")

    def _load_all_accounts(self) -> list[dict[str, Any]]:
        data = load_cfmail_accounts_from_file(self.config_path, silent=False)
        return [item for item in data if isinstance(item, dict)]

    def _write_accounts(self, accounts: list[dict[str, Any]]) -> None:
        payload = {"accounts": accounts}
        tmp_path = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(self.config_path)

    def _pick_active_domain(self, accounts: list[dict[str, Any]]) -> str:
        for item in reversed(accounts):
            domain = str(item.get("email_domain") or "").strip().lower()
            if domain and item.get("enabled", True):
                return domain
        return ""

    def normalize_accounts_to_single_active_domain(self) -> dict[str, Any]:
        accounts = self._load_all_accounts()
        active_domain = self._pick_active_domain(accounts)
        if not active_domain:
            return {"active_domain": "", "removed_domains": []}
        managed_domains = set(self._managed_auto_domains(accounts))
        previous_managed_domain = ""
        for item in reversed(accounts):
            domain = str(item.get("email_domain") or "").strip().lower()
            if not domain or domain == active_domain or domain not in managed_domains:
                continue
            previous_managed_domain = domain
            break
        normalized_accounts: list[dict[str, Any]] = []
        removed_domains: list[str] = []
        changed = False
        for item in accounts:
            domain = str(item.get("email_domain") or "").strip().lower()
            if domain == active_domain:
                if item.get("enabled") is not True:
                    changed = True
                item["enabled"] = True
                normalized_accounts.append(item)
                continue
            if domain == previous_managed_domain:
                if item.get("enabled") is not False:
                    changed = True
                item["enabled"] = False
                normalized_accounts.append(item)
                continue
            if domain in managed_domains:
                removed_domains.append(domain)
                changed = True
                continue
            if item.get("enabled") is not False:
                changed = True
            item["enabled"] = False
            normalized_accounts.append(item)
        if changed or len(normalized_accounts) != len(accounts):
            self._write_accounts(normalized_accounts)
        for domain in removed_domains:
            try:
                self._delete_domain_artifacts(domain)
            except Exception:
                pass
        return {"active_domain": active_domain, "removed_domains": removed_domains}

    def switch_active_domain(self, *, old_domain: str, new_domain: str, worker_domain: str, admin_password: str) -> list[str]:
        accounts = self._load_all_accounts()
        managed_domains = set(self._managed_auto_domains(accounts))
        old_domain_key = str(old_domain or "").strip().lower()
        replacement = {
            "name": f"cfmail-{new_domain.split('.', 1)[0]}",
            "worker_domain": _normalize_host(worker_domain),
            "email_domain": new_domain,
            "admin_password": admin_password,
            "enabled": True,
        }
        normalized_accounts: list[dict[str, Any]] = []
        removed_domains: list[str] = []
        matched = False
        for item in accounts:
            domain = str(item.get("email_domain") or "").strip().lower()
            if domain == new_domain.lower():
                item.update(replacement)
                item["enabled"] = True
                normalized_accounts.append(item)
                matched = True
                continue
            if domain == old_domain_key:
                item["enabled"] = False
                normalized_accounts.append(item)
                continue
            if domain in managed_domains:
                removed_domains.append(domain)
                continue
            item["enabled"] = False
            normalized_accounts.append(item)
        if not matched:
            normalized_accounts.append(replacement)
        self._write_accounts(normalized_accounts)
        return sorted(set(domain for domain in removed_domains if domain and domain != new_domain.lower()))

    def current_active_account(self) -> dict[str, Any]:
        accounts = self._load_all_accounts()
        active_domain = self._pick_active_domain(accounts)
        for item in reversed(accounts):
            if str(item.get("email_domain") or "").strip().lower() == active_domain:
                return item
        raise RuntimeError("no active cfmail account found")

    def rotate_active_domain(self) -> ProvisionResult:
        current = self.current_active_account()
        old_domain = str(current.get("email_domain") or "").strip().lower()
        worker_domain = str(current.get("worker_domain") or "").strip()
        admin_password = str(current.get("admin_password") or "").strip()
        if not old_domain or not worker_domain or not admin_password:
            return ProvisionResult(success=False, step="load_active_account", error="active cfmail account incomplete")
        last_error = ""
        last_new_domain = ""
        for attempt in range(2):
            label = self._make_new_label()
            new_domain = self._new_domain(label)
            last_new_domain = new_domain
            try:
                self._create_email_routing_rule(new_domain, label)
                self._create_dns_records(new_domain)
                self._update_worker_domains(new_domain, old_domain=old_domain)
                self.smoke_test(worker_domain, admin_password, new_domain)
                self.switch_active_domain(
                    old_domain=old_domain,
                    new_domain=new_domain,
                    worker_domain=worker_domain,
                    admin_password=admin_password,
                )
                try:
                    self.cleanup_stale_cf_resources(keep_domains={old_domain})
                except Exception:
                    pass  # cleanup is best-effort, must not abort rotation
                return ProvisionResult(
                    success=True,
                    step="completed",
                    old_domain=old_domain,
                    new_domain=new_domain,
                )
            except Exception as exc:
                last_error = str(exc)
                try:
                    self._delete_domain_artifacts(new_domain)
                except Exception:
                    pass
                if attempt == 0 and self._is_record_quota_error(exc):
                    cleanup_result = self.cleanup_stale_cf_resources()
                    if (
                        cleanup_result.get("removed_domains")
                        or cleanup_result.get("removed_dns_records")
                        or cleanup_result.get("removed_routing_rules")
                    ):
                        continue
                break
        return ProvisionResult(
            success=False,
            step="failed",
            old_domain=old_domain,
            new_domain=last_new_domain,
            error=last_error,
        )

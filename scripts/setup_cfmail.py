from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
from typing import Any

from core.cfmail_provisioner import CfmailProvisioner, ProvisioningSettings

DEFAULT_WORKER_REPO = "https://github.com/dreamhunter2333/cloudflare_temp_email.git"
DEFAULT_WORKER_NAME = "zhuce6-cfmail"
DEFAULT_D1_NAME = "zhuce6-cfmail-db"
DEFAULT_VENDOR_DIR = Path("vendor")
DEFAULT_WORKER_DIR = DEFAULT_VENDOR_DIR / "cfmail-worker"
DEFAULT_CONFIG_DIR = Path("config")
DEFAULT_CFMAIL_ACCOUNTS_PATH = DEFAULT_CONFIG_DIR / "cfmail_accounts.json"
DEFAULT_CFMAIL_ENV_PATH = DEFAULT_CONFIG_DIR / "cfmail_provision.env"
DEFAULT_COMPATIBILITY_DATE = "2025-04-01"
EMAIL_ROUTING_FALLBACK_MX_RECORDS = (
    ("amir.mx.cloudflare.net", 13),
    ("isaac.mx.cloudflare.net", 24),
    ("linda.mx.cloudflare.net", 86),
)
EMAIL_ROUTING_FALLBACK_SPF = "v=spf1 include:_spf.mx.cloudflare.net ~all"
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class SetupError(RuntimeError):
    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


@dataclass(frozen=True)
class WorkerLayout:
    repo_dir: Path
    worker_dir: Path
    schema_path: Path
    migration_paths: tuple[Path, ...]
    wrangler_template_path: Path | None


@dataclass(frozen=True)
class DNSRecordSpec:
    record_type: str
    name: str
    content: str
    priority: int | None = None
    ttl: int = 1
    proxied: bool | None = None


@dataclass(frozen=True)
class CfmailRuntimeConfig:
    api_token: str
    account_id: str
    zone_id: str
    worker_name: str
    worker_domain: str
    zone_name: str
    email_domain: str
    admin_password: str
    d1_name: str
    d1_database_id: str


class CloudflareClient:
    def __init__(
        self,
        api_token: str,
        *,
        auth_email: str = "",
        auth_key: str = "",
        timeout: float = 30.0,
    ) -> None:
        api_token = str(api_token or "").strip()
        auth_email = str(auth_email or "").strip()
        auth_key = str(auth_key or "").strip()
        if not api_token and not (auth_email and auth_key):
            raise SetupError(
                "缺少 Cloudflare 凭据。",
                hint="请提供 Cloudflare API Token, 或提供 CF_AUTH_EMAIL + CF_AUTH_KEY。",
            )
        try:
            import httpx  # type: ignore
        except ImportError as exc:
            raise SetupError(
                "当前 Python 环境缺少 httpx。",
                hint="请先执行 `uv sync` 或 `uv pip install httpx`。",
            ) from exc

        self._httpx = httpx
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "zhuce6/setup_cfmail",
        }
        self._uses_api_token = bool(api_token)
        if api_token:
            headers["Authorization"] = f"Bearer {api_token}"
        else:
            headers["X-Auth-Email"] = auth_email
            headers["X-Auth-Key"] = auth_key
        self._client = httpx.Client(
            base_url="https://api.cloudflare.com/client/v4",
            timeout=timeout,
            trust_env=True,
            headers=headers,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "CloudflareClient":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                response = self._client.request(method, path, params=params, json=json_body)
            except self._httpx.HTTPError as exc:
                last_error = exc
                if attempt < 3:
                    continue
                raise SetupError(
                    f"Cloudflare API 请求失败: {method.upper()} {path}",
                    hint=f"请检查网络连通性后重试。原始错误: {exc}",
                ) from exc
            if response.status_code in RETRYABLE_STATUS_CODES and attempt < 3:
                continue
            try:
                payload = response.json()
            except ValueError as exc:
                raise SetupError(
                    f"Cloudflare API 返回了无法解析的 JSON: {method.upper()} {path}",
                    hint=f"HTTP {response.status_code}, 响应片段: {response.text[:300]}",
                ) from exc
            if response.is_success and payload.get("success") is True:
                return payload

            errors = payload.get("errors") or []
            message = "; ".join(
                str(item.get("message") or item.get("code") or item)
                for item in errors
                if item
            ).strip()
            if not message:
                message = response.text[:300].strip() or f"HTTP {response.status_code}"
            raise SetupError(
                f"Cloudflare API 调用失败: {method.upper()} {path} -> {message}",
                hint=self._build_api_hint(path, response.status_code),
            )
        if last_error is not None:
            raise SetupError(str(last_error))
        raise SetupError(f"Cloudflare API 调用失败: {method.upper()} {path}")

    def _build_api_hint(self, path: str, status_code: int) -> str:
        if status_code in {401, 403}:
            return (
                "请确认 API Token 具备 Zone Read, DNS Edit, Workers Scripts Write, D1 Edit, "
                "以及 Email Routing 写权限。"
            )
        if "/email/routing" in path:
            return "请先在 Cloudflare Dashboard 手动开启 Email Routing, 然后重新执行脚本。"
        return "请根据 Cloudflare 返回信息检查配置后重试。"

    def verify_token(self, *, account_id: str = "") -> dict[str, Any]:
        if self._uses_api_token:
            # Try user-level verify first; fall back to account-scoped verify
            # for account-scoped API tokens.
            try:
                payload = self.request("GET", "/user/tokens/verify")
                result = payload.get("result")
                if isinstance(result, dict):
                    return result
            except SetupError:
                pass
            if account_id:
                payload = self.request("GET", f"/accounts/{account_id}/tokens/verify")
                result = payload.get("result")
                if isinstance(result, dict):
                    return result
            raise SetupError("Token 校验响应缺少 result 字段。")

        payload = self.request("GET", "/user")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise SetupError("Cloudflare 用户校验响应缺少 result 字段。")
        if not result.get("status"):
            result = {**result, "status": "active"}
        return result

    def resolve_zone(self, zone_name: str) -> dict[str, Any]:
        payload = self.request("GET", "/zones", params={"name": zone_name})
        result = payload.get("result") or []
        matches = [item for item in result if isinstance(item, dict) and item.get("name") == zone_name]
        if not matches:
            raise SetupError(
                f"未找到 zone: {zone_name}",
                hint="请确认该域名已接入当前 Cloudflare 账号, 且 API Token 有 Zone Read 权限。",
            )
        zone = matches[0]
        account = zone.get("account") if isinstance(zone.get("account"), dict) else {}
        account_id = str(account.get("id") or "").strip()
        zone_id = str(zone.get("id") or "").strip()
        if not account_id or not zone_id:
            raise SetupError("Zone 信息中缺少 account_id 或 zone_id。")
        return zone

    def list_d1_databases(self, account_id: str, *, database_name: str = "") -> list[dict[str, Any]]:
        payload = self.request("GET", f"/accounts/{account_id}/d1/database")
        result = payload.get("result") or []
        items = [item for item in result if isinstance(item, dict)]
        if database_name:
            items = [item for item in items if str(item.get("name") or "") == database_name]
        return items

    def ensure_d1_database(self, account_id: str, database_name: str) -> dict[str, Any]:
        existing = self.list_d1_databases(account_id, database_name=database_name)
        if existing:
            return existing[0]
        payload = self.request(
            "POST",
            f"/accounts/{account_id}/d1/database",
            json_body={"name": database_name},
        )
        result = payload.get("result")
        if not isinstance(result, dict):
            raise SetupError("创建 D1 数据库成功, 但响应缺少 result。")
        return result

    def get_workers_subdomain(self, account_id: str) -> str:
        payload = self.request("GET", f"/accounts/{account_id}/workers/subdomain")
        result = payload.get("result")
        if not isinstance(result, dict):
            return ""
        return str(result.get("subdomain") or "").strip()

    def get_email_routing_status(self, zone_id: str) -> dict[str, Any]:
        payload = self.request("GET", f"/zones/{zone_id}/email/routing")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise SetupError("Email Routing 状态接口返回格式异常。")
        return result

    def get_email_routing_dns_requirements(self, zone_id: str) -> list[DNSRecordSpec]:
        payload = self.request("GET", f"/zones/{zone_id}/email/routing/dns")
        result = payload.get("result")
        items: list[dict[str, Any]] = []
        if isinstance(result, list):
            items = [item for item in result if isinstance(item, dict)]
        elif isinstance(result, dict):
            for key in ("records", "items", "dns_records", "dns"):
                value = result.get(key)
                if isinstance(value, list):
                    items = [item for item in value if isinstance(item, dict)]
                    break
        records: list[DNSRecordSpec] = []
        for item in items:
            record_type = str(item.get("type") or item.get("record_type") or "").upper()
            name = str(item.get("name") or item.get("hostname") or "").strip()
            content = str(item.get("content") or item.get("value") or "").strip()
            if not record_type or not name or not content:
                continue
            priority = item.get("priority")
            try:
                parsed_priority = int(priority) if priority is not None else None
            except (TypeError, ValueError):
                parsed_priority = None
            records.append(
                DNSRecordSpec(
                    record_type=record_type,
                    name=name,
                    content=content,
                    priority=parsed_priority,
                    ttl=int(item.get("ttl") or 1),
                    proxied=item.get("proxied") if isinstance(item.get("proxied"), bool) else None,
                )
            )
        return records

    def list_dns_records(self, zone_id: str, *, name: str = "", record_type: str = "") -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if name:
            params["name"] = name
        if record_type:
            params["type"] = record_type
        payload = self.request("GET", f"/zones/{zone_id}/dns_records", params=params)
        result = payload.get("result") or []
        return [item for item in result if isinstance(item, dict)]

    def ensure_dns_record(self, zone_id: str, spec: DNSRecordSpec) -> dict[str, Any]:
        existing = self.list_dns_records(zone_id, name=spec.name, record_type=spec.record_type)
        for record in existing:
            if self._dns_record_matches(record, spec):
                return record

        updatable = self._select_updatable_dns_record(existing, spec)
        payload = {
            "type": spec.record_type,
            "name": spec.name,
            "content": spec.content,
            "ttl": spec.ttl,
        }
        if spec.priority is not None:
            payload["priority"] = spec.priority
        if spec.proxied is not None and spec.record_type not in {"MX", "TXT"}:
            payload["proxied"] = spec.proxied

        if updatable is not None:
            response = self.request(
                "PUT",
                f"/zones/{zone_id}/dns_records/{updatable['id']}",
                json_body=payload,
            )
        else:
            response = self.request("POST", f"/zones/{zone_id}/dns_records", json_body=payload)
        result = response.get("result")
        if not isinstance(result, dict):
            raise SetupError(f"DNS 记录写入成功, 但响应格式异常: {spec.record_type} {spec.name}")
        return result

    def _dns_record_matches(self, record: dict[str, Any], spec: DNSRecordSpec) -> bool:
        if str(record.get("type") or "").upper() != spec.record_type:
            return False
        if str(record.get("name") or "").strip().lower() != spec.name.lower():
            return False
        if str(record.get("content") or "").strip().lower() != spec.content.lower():
            return False
        if spec.priority is not None and int(record.get("priority") or 0) != spec.priority:
            return False
        return True

    def _select_updatable_dns_record(self, existing: list[dict[str, Any]], spec: DNSRecordSpec) -> dict[str, Any] | None:
        if spec.record_type == "TXT":
            spf_like = [
                record for record in existing
                if str(record.get("content") or "").strip().lower().startswith("v=spf1")
            ]
            if len(spf_like) == 1:
                return spf_like[0]
            return None
        for record in existing:
            if str(record.get("content") or "").strip().lower() == spec.content.lower():
                return record
        return None

    def get_catch_all_rule(self, zone_id: str) -> dict[str, Any] | None:
        try:
            payload = self.request("GET", f"/zones/{zone_id}/email/routing/rules/catch_all")
        except SetupError as exc:
            if "not found" in str(exc).lower():
                return None
            raise
        result = payload.get("result")
        return result if isinstance(result, dict) else None

    def ensure_catch_all_worker(self, zone_id: str, worker_name: str) -> dict[str, Any]:
        current = self.get_catch_all_rule(zone_id) or {}
        desired_actions = [{"type": "worker", "value": [worker_name]}]
        desired_matchers = [{"type": "all"}]
        if (
            bool(current.get("enabled", True))
            and self._normalize_actions(current.get("actions")) == desired_actions
            and self._normalize_matchers(current.get("matchers")) == desired_matchers
        ):
            return current
        payload = {
            "enabled": True,
            "name": str(current.get("name") or f"{worker_name} catch-all"),
            "matchers": desired_matchers,
            "actions": desired_actions,
        }
        response = self.request(
            "PUT",
            f"/zones/{zone_id}/email/routing/rules/catch_all",
            json_body=payload,
        )
        result = response.get("result")
        if not isinstance(result, dict):
            raise SetupError("Catch-all 规则更新成功, 但响应格式异常。")
        return result

    def _normalize_actions(self, actions: Any) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        if not isinstance(actions, list):
            return normalized
        for item in actions:
            if not isinstance(item, dict):
                continue
            values = item.get("value")
            if isinstance(values, list):
                value_list = [str(v) for v in values]
            elif values is None:
                value_list = []
            else:
                value_list = [str(values)]
            normalized.append({"type": str(item.get("type") or ""), "value": value_list})
        return normalized

    def _normalize_matchers(self, matchers: Any) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        if not isinstance(matchers, list):
            return normalized
        for item in matchers:
            if not isinstance(item, dict):
                continue
            normalized.append({"type": str(item.get("type") or "")})
        return normalized


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="一键部署 cfmail Worker 并生成 zhuce6 配置。")
    parser.add_argument("--api-token", default="", help="Cloudflare API Token")
    parser.add_argument("--auth-email", default="", help="Cloudflare 认证邮箱, 与 --auth-key 成对使用")
    parser.add_argument("--auth-key", default="", help="Cloudflare Global API Key, 与 --auth-email 成对使用")
    parser.add_argument("--zone-name", required=True, help="Cloudflare Zone 名称, 例如 example.com")
    parser.add_argument("--worker-name", default=DEFAULT_WORKER_NAME, help=f"Worker 名称, 默认 {DEFAULT_WORKER_NAME}")
    parser.add_argument("--d1-name", default=DEFAULT_D1_NAME, help=f"D1 数据库名称, 默认 {DEFAULT_D1_NAME}")
    parser.add_argument("--mail-domain", help="邮箱域名, 默认等于 --zone-name")
    parser.add_argument(
        "--skip-clone",
        action="store_true",
        help="若 vendor/cfmail-worker 已存在, 跳过 clone 并直接复用现有目录",
    )
    return parser


def print_step(number: int, total: int, title: str) -> None:
    print(f"[{number}/{total}] {title}")


def ensure_command(name: str, *, install_hint: str) -> None:
    if shutil.which(name):
        return
    raise SetupError(f"缺少必要命令: {name}", hint=install_hint)


def ensure_required_tools() -> None:
    ensure_command("git", install_hint="请先安装 git, 然后重新执行脚本。")
    ensure_command("node", install_hint="请先安装 Node.js 18+。")
    ensure_command("npm", install_hint="请先安装 npm。")
    ensure_command("npx", install_hint="请先安装 npm, 确保 npx 可用。")


def ensure_mail_domain(zone_name: str, mail_domain: str) -> str:
    zone_name = str(zone_name or "").strip().lower()
    mail_domain = str(mail_domain or zone_name).strip().lower()
    if not mail_domain:
        raise SetupError("mail_domain 不能为空。")
    if mail_domain != zone_name and not mail_domain.endswith(f".{zone_name}"):
        raise SetupError(
            f"mail_domain 必须等于 zone_name 或属于其子域: {mail_domain}",
            hint=f"当前 zone_name 为 {zone_name}, 请改用 {zone_name} 或其子域。",
        )
    return mail_domain


def clone_worker_source(target_dir: Path, *, skip_clone: bool) -> Path:
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    if target_dir.exists():
        if not (target_dir / ".git").exists():
            raise SetupError(
                f"目标目录已存在但不是 git 仓库: {target_dir}",
                hint="请删除该目录后重试, 或改用干净的 vendor/cfmail-worker 路径。",
            )
        if skip_clone:
            print(f"    复用现有源码目录: {target_dir}")
            return target_dir
        print(f"    检测到现有源码目录, 直接复用: {target_dir}")
        return target_dir
    run_command(
        ["git", "clone", "--depth", "1", DEFAULT_WORKER_REPO, str(target_dir)],
        cwd=Path.cwd(),
        step="clone Worker 源码",
    )
    return target_dir


def resolve_worker_layout(repo_dir: Path) -> WorkerLayout:
    worker_dir = repo_dir / "worker"
    if not worker_dir.exists():
        raise SetupError(
            f"上游源码缺少 worker 目录: {worker_dir}",
            hint="请检查上游仓库结构是否变化。",
        )
    wrangler_template_path: Path | None = None
    for candidate in (worker_dir / "wrangler.toml.template", worker_dir / "wrangler.toml"):
        if candidate.exists():
            wrangler_template_path = candidate
            break

    schema_candidates = (
        repo_dir / "db" / "schema.sql",
        worker_dir / "schema.sql",
    )
    schema_path = next((path for path in schema_candidates if path.exists()), None)
    if schema_path is None:
        raise SetupError("未找到 schema.sql。", hint="请检查上游仓库结构是否变化。")

    migration_paths: list[Path] = []
    migration_dirs = (repo_dir / "db", worker_dir / "migrations")
    for directory in migration_dirs:
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.sql")):
            if path.resolve() == schema_path.resolve():
                continue
            migration_paths.append(path)
        if migration_paths:
            break

    return WorkerLayout(
        repo_dir=repo_dir,
        worker_dir=worker_dir,
        schema_path=schema_path,
        migration_paths=tuple(migration_paths),
        wrangler_template_path=wrangler_template_path,
    )


def read_wrangler_template_defaults(template_path: Path | None) -> dict[str, Any]:
    defaults = {
        "main": "src/worker.ts",
        "compatibility_date": DEFAULT_COMPATIBILITY_DATE,
        "compatibility_flags": ["nodejs_compat"],
        "keep_vars": True,
    }
    if template_path is None or not template_path.exists():
        return defaults
    try:
        import tomllib

        parsed = tomllib.loads(template_path.read_text(encoding="utf-8"))
    except Exception:
        return defaults
    defaults["main"] = str(parsed.get("main") or defaults["main"])
    defaults["compatibility_date"] = str(parsed.get("compatibility_date") or defaults["compatibility_date"])
    flags = parsed.get("compatibility_flags")
    if isinstance(flags, list) and flags:
        defaults["compatibility_flags"] = [str(item) for item in flags]
    defaults["keep_vars"] = bool(parsed.get("keep_vars", defaults["keep_vars"]))
    return defaults


def toml_string(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def toml_array(values: list[str]) -> str:
    return json.dumps([str(value) for value in values], ensure_ascii=False)


def write_worker_wrangler(
    *,
    worker_dir: Path,
    worker_name: str,
    account_id: str,
    database_id: str,
    database_name: str,
    email_domain: str,
    admin_password: str,
    jwt_secret: str,
    compatibility_date: str = DEFAULT_COMPATIBILITY_DATE,
    main: str = "src/worker.ts",
    compatibility_flags: list[str] | None = None,
    keep_vars: bool = True,
) -> Path:
    wrangler_path = worker_dir / "wrangler.toml"
    flags = compatibility_flags or ["nodejs_compat"]
    content = "\n".join(
        [
            f"name = {toml_string(worker_name)}",
            f"account_id = {toml_string(account_id)}",
            f"main = {toml_string(main)}",
            f"compatibility_date = {toml_string(compatibility_date)}",
            f"compatibility_flags = {toml_array(flags)}",
            f"keep_vars = {'true' if keep_vars else 'false'}",
            "",
            "[vars]",
            f"PREFIX = {toml_string('tmp')}",
            f"DEFAULT_DOMAINS = {toml_array([email_domain])}",
            f"DOMAINS = {toml_array([email_domain])}",
            f"ADMIN_PASSWORDS = {toml_array([admin_password])}",
            f"JWT_SECRET = {toml_string(jwt_secret)}",
            "ENABLE_USER_CREATE_EMAIL = true",
            "ENABLE_USER_DELETE_EMAIL = true",
            "ENABLE_AUTO_REPLY = false",
            "",
            "[[d1_databases]]",
            f"binding = {toml_string('DB')}",
            f"database_name = {toml_string(database_name)}",
            f"database_id = {toml_string(database_id)}",
            "",
        ]
    )
    wrangler_path.write_text(content, encoding="utf-8")
    return wrangler_path


def render_cfmail_accounts_payload(
    *,
    worker_domain: str,
    email_domain: str,
    worker_name: str,
    admin_password: str,
) -> list[dict[str, Any]]:
    return [
        {
            "name": worker_name,
            "worker_domain": worker_domain,
            "email_domain": email_domain,
            "admin_password": admin_password,
            "enabled": True,
        }
    ]


def write_cfmail_accounts_json(
    output_path: Path,
    *,
    worker_domain: str,
    email_domain: str,
    worker_name: str,
    admin_password: str,
) -> Path:
    payload = render_cfmail_accounts_payload(
        worker_domain=worker_domain,
        email_domain=email_domain,
        worker_name=worker_name,
        admin_password=admin_password,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output_path


def shell_quote(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def write_cfmail_provision_env(
    output_path: Path,
    *,
    api_token: str = "",
    auth_email: str = "",
    auth_key: str = "",
    account_id: str,
    zone_id: str,
    worker_name: str,
    zone_name: str,
    d1_database_id: str = "",
) -> Path:
    content = "\n".join(
        [
            f'export ZHUCE6_CFMAIL_API_TOKEN="{shell_quote(api_token)}"',
            f'export ZHUCE6_CFMAIL_CF_AUTH_EMAIL="{shell_quote(auth_email)}"',
            f'export ZHUCE6_CFMAIL_CF_AUTH_KEY="{shell_quote(auth_key)}"',
            f'export ZHUCE6_CFMAIL_CF_ACCOUNT_ID="{shell_quote(account_id)}"',
            f'export ZHUCE6_CFMAIL_CF_ZONE_ID="{shell_quote(zone_id)}"',
            f'export ZHUCE6_CFMAIL_WORKER_NAME="{shell_quote(worker_name)}"',
            f'export ZHUCE6_CFMAIL_ZONE_NAME="{shell_quote(zone_name)}"',
            f'export ZHUCE6_D1_DATABASE_ID="{shell_quote(d1_database_id)}"',
            "",
        ]
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    return output_path


def prepare_runtime_cfmail_config(
    *,
    api_token: str,
    auth_email: str = "",
    auth_key: str = "",
    worker_domain: str | None = None,
    zone_name: str,
    worker_name: str = DEFAULT_WORKER_NAME,
    d1_name: str = DEFAULT_D1_NAME,
    mail_domain: str | None = None,
    admin_password: str | None = None,
    accounts_path: Path = DEFAULT_CFMAIL_ACCOUNTS_PATH,
    provision_env_path: Path = DEFAULT_CFMAIL_ENV_PATH,
) -> CfmailRuntimeConfig:
    normalized_zone_name = str(zone_name or "").strip().lower()
    if not normalized_zone_name:
        raise SetupError("zone_name 不能为空。")
    normalized_mail_domain = ensure_mail_domain(normalized_zone_name, mail_domain or normalized_zone_name)
    normalized_worker_name = str(worker_name or DEFAULT_WORKER_NAME).strip() or DEFAULT_WORKER_NAME
    normalized_worker_domain = str(worker_domain or "").strip().lower()
    resolved_admin_password = str(admin_password or "").strip() or secrets.token_urlsafe(24)

    with CloudflareClient(api_token, auth_email=auth_email, auth_key=auth_key) as client:
        # Resolve zone first to obtain account_id for account-scoped tokens.
        zone = client.resolve_zone(normalized_zone_name)
        _account_for_verify = (zone.get("account") or {}).get("id", "") if isinstance(zone.get("account"), dict) else ""
        client.verify_token(account_id=str(_account_for_verify))
        zone_id = str(zone.get("id") or "").strip()
        account = zone.get("account") if isinstance(zone.get("account"), dict) else {}
        account_id = str(account.get("id") or "").strip()
        database = client.ensure_d1_database(account_id, d1_name)
        d1_database_id = str(database.get("uuid") or database.get("id") or "").strip()
        if not normalized_worker_domain:
            worker_subdomain = client.get_workers_subdomain(account_id)
            normalized_worker_domain = build_worker_domain(normalized_worker_name, worker_subdomain)

    write_cfmail_accounts_json(
        accounts_path,
        worker_domain=normalized_worker_domain,
        email_domain=normalized_mail_domain,
        worker_name=normalized_worker_name,
        admin_password=resolved_admin_password,
    )
    write_cfmail_provision_env(
        provision_env_path,
        api_token=api_token,
        auth_email=auth_email,
        auth_key=auth_key,
        account_id=account_id,
        zone_id=zone_id,
        worker_name=normalized_worker_name,
        zone_name=normalized_zone_name,
        d1_database_id=d1_database_id,
    )
    if normalized_worker_domain:
        provisioner = CfmailProvisioner(
            config_path=accounts_path,
            settings=ProvisioningSettings(
                auth_email=auth_email,
                auth_key=auth_key,
                account_id=account_id,
                zone_id=zone_id,
                worker_name=normalized_worker_name,
                zone_name=normalized_zone_name,
            ),
        )
        try:
            provisioner.smoke_test(normalized_worker_domain, resolved_admin_password, normalized_mail_domain)
        except Exception as exc:
            error_text = str(exc)
            if "无效的域名" not in error_text and "invalid" not in error_text.lower():
                raise
            rotation = provisioner.rotate_active_domain()
            if not rotation.success or not rotation.new_domain:
                raise SetupError(
                    "cfmail 域名校验失败, 且自动轮换未成功。",
                    hint=rotation.error or error_text or "请确认 worker_domain 与 zone_name 配置正确。",
                )
            normalized_mail_domain = rotation.new_domain

    return CfmailRuntimeConfig(
        api_token=api_token,
        account_id=account_id,
        zone_id=zone_id,
        worker_name=normalized_worker_name,
        worker_domain=normalized_worker_domain,
        zone_name=normalized_zone_name,
        email_domain=normalized_mail_domain,
        admin_password=resolved_admin_password,
        d1_name=d1_name,
        d1_database_id=d1_database_id,
    )


def run_command(
    args: list[str],
    *,
    cwd: Path,
    step: str,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    process = subprocess.run(
        args,
        cwd=str(cwd),
        env=merged_env,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        detail = (process.stderr or process.stdout or "").strip()
        raise SetupError(
            f"{step} 失败: {' '.join(args)}",
            hint=detail[:1200] or "请根据命令输出检查后重试。",
        )
    return process


def is_benign_migration_error(detail: str) -> bool:
    normalized = str(detail or "").lower()
    markers = (
        "duplicate column name",
        "already exists",
        "no such table",
        "duplicate index name",
    )
    return any(marker in normalized for marker in markers)


def run_wrangler_sql_file(
    *,
    database_name: str,
    sql_path: Path,
    cwd: Path,
    env: dict[str, str],
    step: str,
    tolerate_already_applied: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        return run_command(
            ["npx", "wrangler", "d1", "execute", database_name, "--remote", "--file", str(sql_path)],
            cwd=cwd,
            env=env,
            step=step,
        )
    except SetupError as exc:
        if tolerate_already_applied and is_benign_migration_error(exc.hint):
            print(f"    跳过已存在或历史补丁不再适用的 migration: {sql_path.name}")
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=exc.hint)
        raise


def build_wrangler_env(api_token: str) -> dict[str, str]:
    return {
        "CLOUDFLARE_API_TOKEN": api_token,
        "CF_API_TOKEN": api_token,
        "CI": "1",
        "NO_UPDATE_NOTIFIER": "1",
        "npm_config_update_notifier": "false",
    }


def ensure_email_routing_dns(client: CloudflareClient, zone_id: str, mail_domain: str) -> list[DNSRecordSpec]:
    try:
        specs = client.get_email_routing_dns_requirements(zone_id)
    except SetupError:
        specs = []
    filtered = [spec for spec in specs if spec.record_type in {"MX", "TXT"}]
    if not filtered:
        filtered = [
            DNSRecordSpec("MX", mail_domain, host, priority=priority)
            for host, priority in EMAIL_ROUTING_FALLBACK_MX_RECORDS
        ]
        filtered.append(DNSRecordSpec("TXT", mail_domain, EMAIL_ROUTING_FALLBACK_SPF))

    normalized: list[DNSRecordSpec] = []
    for spec in filtered:
        normalized_name = spec.name.rstrip(".")
        if normalized_name == "@":
            normalized_name = mail_domain
        if not normalized_name:
            normalized_name = mail_domain
        normalized.append(
            DNSRecordSpec(
                record_type=spec.record_type,
                name=normalized_name,
                content=spec.content.rstrip("."),
                priority=spec.priority,
                ttl=spec.ttl,
                proxied=spec.proxied,
            )
        )
    for spec in normalized:
        client.ensure_dns_record(zone_id, spec)
    return normalized


def email_routing_enabled(status: dict[str, Any]) -> bool:
    for key in ("enabled", "active"):
        value = status.get(key)
        if isinstance(value, bool):
            return value
    state = str(status.get("status") or status.get("state") or "").strip().lower()
    return state in {"active", "enabled", "ready", "verified", "success"}


def build_worker_domain(worker_name: str, account_subdomain: str) -> str:
    worker_name = str(worker_name or "").strip()
    account_subdomain = str(account_subdomain or "").strip()
    if not worker_name or not account_subdomain:
        raise SetupError(
            "无法推导 workers.dev 域名。",
            hint="请确认账号已启用 workers.dev 子域, 或在 Cloudflare Dashboard 中先完成一次 Worker 初始化。",
        )
    return f"{worker_name}.{account_subdomain}.workers.dev"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    zone_name = str(args.zone_name).strip().lower()
    worker_name = str(args.worker_name).strip()
    d1_name = str(args.d1_name).strip()
    mail_domain = ensure_mail_domain(zone_name, args.mail_domain or zone_name)
    api_token = str(args.api_token).strip()
    auth_email = str(args.auth_email).strip()
    auth_key = str(args.auth_key).strip()
    if not api_token and not (auth_email and auth_key):
        raise SystemExit("ERROR: 请提供 --api-token, 或同时提供 --auth-email 和 --auth-key。")
    vendor_dir = Path.cwd() / DEFAULT_WORKER_DIR
    cfmail_accounts_path = Path.cwd() / DEFAULT_CFMAIL_ACCOUNTS_PATH
    cfmail_env_path = Path.cwd() / DEFAULT_CFMAIL_ENV_PATH

    total_steps = 13
    try:
        print_step(1, total_steps, "检查本地依赖")
        ensure_required_tools()

        with CloudflareClient(api_token, auth_email=auth_email, auth_key=auth_key) as client:
            print_step(2, total_steps, "解析 zone/account 信息")
            zone = client.resolve_zone(zone_name)
            zone_id = str(zone.get("id") or "").strip()
            account = zone.get("account") if isinstance(zone.get("account"), dict) else {}
            account_id = str(account.get("id") or "").strip()

            print_step(3, total_steps, "验证 Cloudflare 凭据")
            token_info = client.verify_token(account_id=account_id)
            print(f"    token_status={token_info.get('status', 'unknown')}")
            print(f"    account_id={account_id}")
            print(f"    zone_id={zone_id}")

            print_step(4, total_steps, "准备 Worker 源码")
            repo_dir = clone_worker_source(vendor_dir, skip_clone=bool(args.skip_clone))
            layout = resolve_worker_layout(repo_dir)
            print(f"    worker_dir={layout.worker_dir}")

            print_step(5, total_steps, "创建或复用 D1 数据库")
            database = client.ensure_d1_database(account_id, d1_name)
            database_id = str(database.get("uuid") or database.get("id") or "").strip()
            if not database_id:
                raise SetupError("D1 数据库响应缺少 database_id。")
            print(f"    database_id={database_id}")

            print_step(6, total_steps, "写入 wrangler.toml")
            defaults = read_wrangler_template_defaults(layout.wrangler_template_path)
            admin_password = secrets.token_urlsafe(24)
            jwt_secret = secrets.token_urlsafe(48)
            wrangler_path = write_worker_wrangler(
                worker_dir=layout.worker_dir,
                worker_name=worker_name,
                account_id=account_id,
                database_id=database_id,
                database_name=d1_name,
                email_domain=mail_domain,
                admin_password=admin_password,
                jwt_secret=jwt_secret,
                compatibility_date=str(defaults.get("compatibility_date") or DEFAULT_COMPATIBILITY_DATE),
                main=str(defaults.get("main") or "src/worker.ts"),
                compatibility_flags=list(defaults.get("compatibility_flags") or ["nodejs_compat"]),
                keep_vars=bool(defaults.get("keep_vars", True)),
            )
            print(f"    wrote {wrangler_path}")

            wrangler_env = build_wrangler_env(api_token)

            print_step(7, total_steps, "安装 Worker 依赖")
            run_command(["npm", "install", "--no-fund", "--no-audit"], cwd=layout.worker_dir, step="npm install")

            print_step(8, total_steps, "执行 D1 schema 与 migration")
            run_wrangler_sql_file(
                database_name=d1_name,
                sql_path=layout.schema_path,
                cwd=layout.worker_dir,
                env=wrangler_env,
                step="执行 schema.sql",
            )
            for migration_path in layout.migration_paths:
                run_wrangler_sql_file(
                    database_name=d1_name,
                    sql_path=migration_path,
                    cwd=layout.worker_dir,
                    env=wrangler_env,
                    step=f"执行 migration {migration_path.name}",
                    tolerate_already_applied=True,
                )

            print_step(9, total_steps, "部署 Worker")
            run_command(
                ["npx", "wrangler", "deploy", "--minify"],
                cwd=layout.worker_dir,
                env=wrangler_env,
                step="wrangler deploy",
            )

            print_step(10, total_steps, "配置 Email Routing 所需 DNS")
            dns_specs = ensure_email_routing_dns(client, zone_id, mail_domain)
            print(f"    ensured_records={len(dns_specs)}")

            print_step(11, total_steps, "检查 Email Routing 状态并配置 catch-all")
            try:
                routing_status = client.get_email_routing_status(zone_id)
                routing_ok = email_routing_enabled(routing_status)
            except SetupError:
                print("    warning: 无法读取 Email Routing 状态 (account-scoped token), 跳过检查")
                routing_ok = True
            if not routing_ok:
                raise SetupError(
                    "当前 Zone 尚未开启 Email Routing。",
                    hint=(
                        "请先在 Cloudflare Dashboard > Email > Email Routing 中完成启用, "
                        "确认状态变为 active/enabled 后重新执行脚本。"
                    ),
                )
            client.ensure_catch_all_worker(zone_id, worker_name)

            print_step(12, total_steps, "生成 config/cfmail_accounts.json")
            account_subdomain = client.get_workers_subdomain(account_id)
            worker_domain = build_worker_domain(worker_name, account_subdomain)
            write_cfmail_accounts_json(
                cfmail_accounts_path,
                worker_domain=worker_domain,
                email_domain=mail_domain,
                worker_name=worker_name,
                admin_password=admin_password,
            )
            print(f"    wrote {cfmail_accounts_path}")

            print_step(13, total_steps, "生成 config/cfmail_provision.env")
            write_cfmail_provision_env(
                cfmail_env_path,
                api_token=api_token,
                auth_email=auth_email,
                auth_key=auth_key,
                account_id=account_id,
                zone_id=zone_id,
                worker_name=worker_name,
                zone_name=zone_name,
                d1_database_id=database_id,
            )
            print(f"    wrote {cfmail_env_path}")

        print("部署完成。")
        return 0
    except SetupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if exc.hint:
            print(f"HINT: {exc.hint}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("ERROR: 用户中断执行。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

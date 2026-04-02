"""Interactive environment bootstrap for zhuce6."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import shutil
from typing import Callable
from urllib.parse import urlparse

import httpx

from core.cfmail import enabled_cfmail_accounts
from core.doctor import sslocal_install_guidance
from scripts import setup_cfmail

InputFn = Callable[[str], str]
PrintFn = Callable[[str], None]


@dataclass(frozen=True)
class SetupWizardResult:
    env_file: Path
    env_updates: dict[str, object]
    cfmail_accounts_path: Path | None = None
    cfmail_env_path: Path | None = None


def _load_env_defaults(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        key, sep, value = line.partition("=")
        if not sep:
            continue
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _encode_env_value(value: object) -> str:
    text = "" if value is None else str(value)
    if not text:
        return ""
    if any(ch.isspace() for ch in text) or "#" in text:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def _persist_env_updates(path: Path, updates: dict[str, object]) -> None:
    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    normalized_updates = {key: _encode_env_value(value) for key, value in updates.items()}
    handled: set[str] = set()
    output_lines: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        candidate = stripped[7:] if stripped.startswith("export ") else stripped
        key, sep, _value = candidate.partition("=")
        if sep and key in normalized_updates:
            if key in handled:
                continue
            output_lines.append(f"{key}={normalized_updates[key]}")
            handled.add(key)
            continue
        output_lines.append(line)
    for key, value in normalized_updates.items():
        if key not in handled:
            output_lines.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(output_lines).rstrip() + "\n", encoding="utf-8")


def _prompt_text(
    input_fn: InputFn,
    print_fn: PrintFn,
    label: str,
    *,
    default: str = "",
    required: bool = False,
) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        value = input_fn(f"{label}{suffix}: ").strip()
        if value:
            return value
        if default:
            return default
        if not required:
            return ""
        print_fn(f"{label} 不能为空, 请重新输入.")


def _prompt_bool(input_fn: InputFn, print_fn: PrintFn, label: str, *, default: bool) -> bool:
    default_text = "Y/n" if default else "y/N"
    while True:
        raw = input_fn(f"{label} [{default_text}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes", "1", "true"}:
            return True
        if raw in {"n", "no", "0", "false"}:
            return False
        print_fn("请输入 y 或 n.")


def _prompt_choice(
    input_fn: InputFn,
    print_fn: PrintFn,
    label: str,
    *,
    choices: dict[str, str],
    default: str,
) -> str:
    while True:
        for key, description in choices.items():
            print_fn(f"  {key} = {description}")
        raw = input_fn(f"  {label} [{default}]: ").strip()
        if not raw:
            return default
        if raw in choices:
            return raw
        print_fn(f"请输入 {', '.join(choices)} 之一.")


def _print_step(print_fn: PrintFn, index: int, total: int, title: str) -> None:
    print_fn("")
    print_fn("━" * 40)
    print_fn(f"[{index}/{total}] {title}")
    print_fn("━" * 40)


def _first_cfmail_account_defaults(path: Path) -> dict[str, str]:
    accounts = enabled_cfmail_accounts(path)
    if not accounts:
        return {}
    current = accounts[0]
    return {
        "worker_domain": current.worker_domain,
        "email_domain": current.email_domain,
        "worker_name": current.name,
        "admin_password": current.admin_password,
    }


def _infer_zone_name(email_domain: str) -> str:
    labels = [part for part in str(email_domain or "").strip().split(".") if part]
    if len(labels) >= 2:
        return ".".join(labels[-2:])
    return str(email_domain or "").strip()


def _validate_proxy(print_fn: PrintFn, proxy_url: str) -> None:
    proxy_url = str(proxy_url or "").strip()
    if not proxy_url:
        return
    print_fn(f"  测试代理连通性: {proxy_url}")
    try:
        response = httpx.get("https://api.openai.com", proxy=proxy_url, timeout=10)
        latency_ms = response.elapsed.total_seconds() * 1000
        print_fn(f"  ✅ 连通 (延迟 {latency_ms:.0f}ms)")
    except Exception as exc:  # noqa: BLE001
        if _is_socks_proxy_url(proxy_url) and _is_missing_socks_support(exc):
            print_fn("  ⚠️ 当前环境缺少 SOCKS 依赖, 无法验证该代理.")
            print_fn("  请先运行: uv sync")
            print_fn("  你可以继续, 启动前再补齐依赖.")
            return
        print_fn(f"  ⚠️ 连接失败: {exc}")
        print_fn("  你可以继续, 启动后再排查代理问题.")


def _is_socks_proxy_url(proxy_url: str) -> bool:
    scheme = urlparse(str(proxy_url or "").strip()).scheme.lower()
    return scheme.startswith("socks")


def _is_missing_socks_support(exc: Exception) -> bool:
    message = str(exc).lower()
    return "socksio" in message or "using socks proxy" in message


def _validate_cloudflare_credentials(
    print_fn: PrintFn,
    *,
    api_token: str = "",
    auth_email: str = "",
    auth_key: str = "",
) -> None:
    label = "Cloudflare API Token" if str(api_token or "").strip() else "Cloudflare 全局 Key"
    print_fn(f"  验证 {label}...")
    try:
        with setup_cfmail.CloudflareClient(
            api_token,
            auth_email=auth_email,
            auth_key=auth_key,
            timeout=10,
        ) as client:
            client.verify_token()
    except Exception as exc:  # noqa: BLE001
        print_fn(f"  ⚠️ 验证失败: {exc}")
        print_fn("  你可以继续, 后续再检查 Cloudflare 凭据.")
        return
    print_fn("  ✅ Cloudflare 凭据有效")


def _validate_cpa_management(print_fn: PrintFn, base_url: str) -> None:
    print_fn("  测试连通性...")
    try:
        response = httpx.get(base_url, timeout=10)
        print_fn(f"  ✅ 连通 (HTTP {response.status_code})")
    except Exception as exc:  # noqa: BLE001
        print_fn(f"  ⚠️ 连接失败: {exc}")
        print_fn("  你可以继续, 启动后再排查 CPA 问题.")


def run_setup_wizard(
    env_file: Path | None = None,
    *,
    input_fn: InputFn = input,
    print_fn: PrintFn = print,
) -> SetupWizardResult:
    total_steps = 5
    resolved_env_file = Path(env_file or os.getenv("ZHUCE6_ENV_FILE") or Path.cwd() / ".env").expanduser().resolve()
    env_defaults = _load_env_defaults(resolved_env_file)
    project_root = Path(env_defaults.get("ZHUCE6_PROJECT_ROOT") or resolved_env_file.parent).expanduser().resolve()
    config_dir = Path(env_defaults.get("ZHUCE6_CONFIG_DIR") or project_root / "config").expanduser().resolve()
    cfmail_accounts_path = Path(
        env_defaults.get("ZHUCE6_CFMAIL_CONFIG_PATH") or config_dir / setup_cfmail.DEFAULT_CFMAIL_ACCOUNTS_PATH.name
    ).expanduser().resolve()
    cfmail_env_path = Path(
        env_defaults.get("ZHUCE6_CFMAIL_ENV_FILE") or config_dir / setup_cfmail.DEFAULT_CFMAIL_ENV_PATH.name
    ).expanduser().resolve()
    cfmail_account_defaults = _first_cfmail_account_defaults(cfmail_accounts_path)
    cfmail_env_defaults = _load_env_defaults(cfmail_env_path)

    print_fn("╔══════════════════════════════════════╗")
    print_fn("║       zhuce6 首次配置向导            ║")
    print_fn("╚══════════════════════════════════════╝")
    print_fn("")
    print_fn("直接回车即可接受 [] 中的默认值.")
    print_fn(f"当前 .env 路径: {resolved_env_file}")

    _print_step(print_fn, 1, total_steps, "运行模式与后端")
    mode = _prompt_choice(
        input_fn,
        print_fn,
        "选择模式",
        choices={
            "lite": "仅注册",
            "full": "注册 + 后端治理",
        },
        default=env_defaults.get("ZHUCE6_RUN_MODE", "lite") or "lite",
    )
    backend_default = env_defaults.get("ZHUCE6_BACKEND", "cpa") or "cpa"
    backend = "cpa"
    if mode == "full":
        backend = _prompt_choice(
            input_fn,
            print_fn,
            "选择 full 模式后端",
            choices={
                "cpa": "CPA Management API",
                "sub2api": "sub2api Admin API",
            },
            default=backend_default if backend_default in {"cpa", "sub2api"} else "cpa",
        )

    _print_step(print_fn, 2, total_steps, "Dashboard 配置")
    host = _prompt_text(input_fn, print_fn, "Dashboard host", default=env_defaults.get("ZHUCE6_HOST", "127.0.0.1"))
    port = _prompt_text(input_fn, print_fn, "Dashboard port", default=env_defaults.get("ZHUCE6_PORT", "8000"), required=True)
    register_mail_provider = _prompt_text(
        input_fn,
        print_fn,
        "Register mail provider",
        default=env_defaults.get("ZHUCE6_REGISTER_MAIL_PROVIDER", "cfmail"),
        required=True,
    )

    _print_step(print_fn, 3, total_steps, "代理配置")
    print_fn("  注册需要海外代理 (日本/台湾/新加坡/香港).")
    print_fn('  如果你已有 Clash/V2Ray 在运行, 选 "1" 填 URL 即可.')
    enable_proxy_pool = _prompt_bool(
        input_fn,
        print_fn,
        "Enable proxy pool",
        default=env_defaults.get("ZHUCE6_ENABLE_PROXY_POOL", "1").strip().lower() in {"1", "true", "yes", "on"},
    )
    proxy_pool_config_default = env_defaults.get("ZHUCE6_PROXY_POOL_CONFIG", str(project_root / "clash_config.yaml"))
    proxy_pool_direct_urls_default = env_defaults.get("ZHUCE6_PROXY_POOL_DIRECT_URLS", "")
    register_proxy_default = env_defaults.get("ZHUCE6_REGISTER_PROXY", "http://127.0.0.1:7899")
    proxy_pool_config = ""
    proxy_pool_direct_urls = ""
    register_proxy = register_proxy_default
    validation_proxy_url = ""

    if enable_proxy_pool:
        proxy_pool_mode = _prompt_choice(
            input_fn,
            print_fn,
            "Proxy pool mode",
            choices={
                "1": "直接填代理 URL (推荐)",
                "2": "提供 Clash YAML 配置文件 (需要 sslocal)",
            },
            default="1" if proxy_pool_direct_urls_default.strip() else "2",
        )
        if proxy_pool_mode == "1":
            proxy_pool_direct_urls = _prompt_text(
                input_fn,
                print_fn,
                "代理 URL (多个用分号分隔)",
                default=proxy_pool_direct_urls_default,
                required=True,
            )
            validation_proxy_url = next((item.strip() for item in proxy_pool_direct_urls.split(";") if item.strip()), "")
            proxy_pool_config = ""
            register_proxy = validation_proxy_url or register_proxy_default
            _validate_proxy(print_fn, validation_proxy_url)
        else:
            proxy_pool_config = _prompt_text(
                input_fn,
                print_fn,
                "Clash YAML 配置文件",
                default=proxy_pool_config_default,
                required=True,
            )
            proxy_pool_direct_urls = ""
            register_proxy = register_proxy_default
            sslocal_bin = shutil.which("sslocal") or shutil.which("ss-local")
            if sslocal_bin:
                print_fn(f"  已检测到 sslocal: {sslocal_bin}")
            else:
                print_fn("  未检测到 sslocal, 不会自动安装.")
                for line in sslocal_install_guidance().splitlines():
                    print_fn(line)
    else:
        register_proxy = _prompt_text(
            input_fn,
            print_fn,
            "Register proxy URL",
            default=register_proxy_default,
            required=True,
        )
        validation_proxy_url = register_proxy
        _validate_proxy(print_fn, validation_proxy_url)

    wrote_cfmail = False
    providers = {part.strip().lower() for part in register_mail_provider.split(",") if part.strip()}
    generated_admin_password = secrets.token_hex(8)
    existing_cfmail_available = bool(cfmail_account_defaults) and bool(
        cfmail_env_defaults.get("ZHUCE6_CFMAIL_CF_ACCOUNT_ID", "") and cfmail_env_defaults.get("ZHUCE6_CFMAIL_CF_ZONE_ID", "")
    )

    if "cfmail" in providers:
        _print_step(print_fn, 4, total_steps, "cfmail 邮箱配置")
        print_fn("  cfmail 使用 Cloudflare Worker 接收注册验证码.")
        print_fn("  如果要从零部署 cfmail Worker, 最小输入是 Cloudflare API Token + zone_name.")
        print_fn("  如果只有 CF_AUTH_EMAIL + CF_AUTH_KEY, 则需要额外提供一个已部署的 worker_domain.")
        reuse_existing_cfmail = existing_cfmail_available and _prompt_bool(
            input_fn,
            print_fn,
            "检测到现有 cfmail 配置, 是否直接复用",
            default=True,
        )
        if reuse_existing_cfmail:
            cf_api_token = env_defaults.get("ZHUCE6_CFMAIL_API_TOKEN") or cfmail_env_defaults.get("ZHUCE6_CFMAIL_API_TOKEN", "")
            cf_auth_email = env_defaults.get("ZHUCE6_CFMAIL_CF_AUTH_EMAIL") or cfmail_env_defaults.get("ZHUCE6_CFMAIL_CF_AUTH_EMAIL", "")
            cf_auth_key = env_defaults.get("ZHUCE6_CFMAIL_CF_AUTH_KEY") or cfmail_env_defaults.get("ZHUCE6_CFMAIL_CF_AUTH_KEY", "")
            cf_account_id = env_defaults.get("ZHUCE6_CFMAIL_CF_ACCOUNT_ID") or cfmail_env_defaults.get("ZHUCE6_CFMAIL_CF_ACCOUNT_ID", "")
            cf_zone_id = env_defaults.get("ZHUCE6_CFMAIL_CF_ZONE_ID") or cfmail_env_defaults.get("ZHUCE6_CFMAIL_CF_ZONE_ID", "")
            worker_name = env_defaults.get("ZHUCE6_CFMAIL_WORKER_NAME") or cfmail_env_defaults.get("ZHUCE6_CFMAIL_WORKER_NAME") or cfmail_account_defaults.get("worker_name", setup_cfmail.DEFAULT_WORKER_NAME)
            worker_domain = cfmail_account_defaults.get("worker_domain", "")
            email_domain = cfmail_account_defaults.get("email_domain", env_defaults.get("ZHUCE6_CFMAIL_ZONE_NAME", ""))
            admin_password = cfmail_account_defaults.get("admin_password", generated_admin_password)
            zone_name = env_defaults.get("ZHUCE6_CFMAIL_ZONE_NAME") or cfmail_env_defaults.get("ZHUCE6_CFMAIL_ZONE_NAME") or _infer_zone_name(email_domain)
            d1_database_id = (
                env_defaults.get("ZHUCE6_D1_DATABASE_ID")
                or cfmail_env_defaults.get("ZHUCE6_D1_DATABASE_ID", "")
                or str(os.getenv("ZHUCE6_D1_DATABASE_ID", "")).strip()
            )
            print_fn(f"  复用现有 worker: {worker_name}")
            print_fn(f"  复用现有 domain: {email_domain}")
            wrote_cfmail = True
        else:
            cf_api_token = _prompt_text(
                input_fn,
                print_fn,
                "Cloudflare API Token (留空则改用 CF_AUTH_EMAIL + CF_AUTH_KEY)",
                default=env_defaults.get("ZHUCE6_CFMAIL_API_TOKEN") or cfmail_env_defaults.get("ZHUCE6_CFMAIL_API_TOKEN", ""),
                required=False,
            )
            cf_auth_email = ""
            cf_auth_key = ""
            explicit_worker_domain = ""
            if cf_api_token:
                _validate_cloudflare_credentials(print_fn, api_token=cf_api_token)
            else:
                print_fn("  未提供 API Token, 改用 Cloudflare 全局 Key (CF_AUTH_EMAIL + CF_AUTH_KEY).")
                cf_auth_email = _prompt_text(
                    input_fn,
                    print_fn,
                    "CF_AUTH_EMAIL",
                    default=env_defaults.get("ZHUCE6_CFMAIL_CF_AUTH_EMAIL") or cfmail_env_defaults.get("ZHUCE6_CFMAIL_CF_AUTH_EMAIL", ""),
                    required=True,
                )
                cf_auth_key = _prompt_text(
                    input_fn,
                    print_fn,
                    "CF_AUTH_KEY",
                    default=env_defaults.get("ZHUCE6_CFMAIL_CF_AUTH_KEY") or cfmail_env_defaults.get("ZHUCE6_CFMAIL_CF_AUTH_KEY", ""),
                    required=True,
                )
                _validate_cloudflare_credentials(
                    print_fn,
                    auth_email=cf_auth_email,
                    auth_key=cf_auth_key,
                )
                explicit_worker_domain = _prompt_text(
                    input_fn,
                    print_fn,
                    "已部署 cfmail worker_domain",
                    default=cfmail_account_defaults.get("worker_domain", ""),
                    required=True,
                )
            zone_name = _prompt_text(
                input_fn,
                print_fn,
                "zone_name",
                default=env_defaults.get("ZHUCE6_CFMAIL_ZONE_NAME") or cfmail_env_defaults.get("ZHUCE6_CFMAIL_ZONE_NAME", ""),
                required=True,
            ).lower()
            worker_name = _prompt_text(
                input_fn,
                print_fn,
                "cfmail worker name",
                default=env_defaults.get("ZHUCE6_CFMAIL_WORKER_NAME")
                or cfmail_env_defaults.get("ZHUCE6_CFMAIL_WORKER_NAME")
                or cfmail_account_defaults.get("worker_name", setup_cfmail.DEFAULT_WORKER_NAME),
                required=True,
            )
            email_domain = _prompt_text(
                input_fn,
                print_fn,
                "邮箱域名",
                default=cfmail_account_defaults.get("email_domain", zone_name),
                required=True,
            )
            email_domain = setup_cfmail.ensure_mail_domain(zone_name, email_domain)
            print_fn(f"  admin 密码默认随机生成: {generated_admin_password}")
            admin_password = _prompt_text(
                input_fn,
                print_fn,
                "admin 密码",
                default=cfmail_account_defaults.get("admin_password", generated_admin_password),
                required=True,
            )
            cf_account_id = ""
            cf_zone_id = ""
            worker_domain = explicit_worker_domain
            d1_database_id = ""
            wrote_cfmail = True
    else:
        reuse_existing_cfmail = False
        cf_api_token = ""
        cf_auth_email = ""
        cf_auth_key = ""
        cf_account_id = ""
        cf_zone_id = ""
        worker_name = ""
        email_domain = ""
        zone_name = ""
        d1_database_id = ""

    cfmail_payload = {
        "cf_auth_email": cf_auth_email,
        "cf_auth_key": cf_auth_key,
        "cf_account_id": cf_account_id,
        "cf_zone_id": cf_zone_id,
        "worker_name": worker_name,
        "worker_domain": worker_domain if "cfmail" in providers else "",
        "email_domain": email_domain,
        "admin_password": admin_password if "cfmail" in providers else "",
        "zone_name": zone_name,
        "cf_api_token": cf_api_token if "cfmail" in providers else "",
    }

    cpa_management_base_url = ""
    cpa_management_key = ""
    sub2api_base_url = ""
    sub2api_api_key = ""
    sub2api_admin_email = ""
    sub2api_admin_password = ""
    if mode == "full" and backend == "cpa":
        _print_step(print_fn, 5, total_steps, "CPA 配置")
        print_fn("  full + cpa 走 CPA Management API.")
        cpa_management_base_url = _prompt_text(
            input_fn,
            print_fn,
            "CPA management URL",
            default=env_defaults.get("ZHUCE6_CPA_MANAGEMENT_BASE_URL", "http://127.0.0.1:8317/v0/management"),
            required=True,
        )
        _validate_cpa_management(print_fn, cpa_management_base_url)
        cpa_management_key = _prompt_text(
            input_fn,
            print_fn,
            "CPA management API key",
            default=env_defaults.get("ZHUCE6_CPA_MANAGEMENT_KEY", ""),
            required=False,
        )
    elif mode == "full" and backend == "sub2api":
        _print_step(print_fn, 5, total_steps, "sub2api 配置")
        print_fn("  full + sub2api 走 sub2api Admin API.")
        sub2api_base_url = _prompt_text(
            input_fn,
            print_fn,
            "sub2api base URL",
            default=env_defaults.get("ZHUCE6_SUB2API_BASE_URL", "http://127.0.0.1:8080"),
            required=True,
        )
        sub2api_auth_mode = _prompt_choice(
            input_fn,
            print_fn,
            "sub2api 认证方式",
            choices={
                "api_key": "使用 API Key",
                "password": "使用管理员邮箱 + 密码",
            },
            default="api_key" if env_defaults.get("ZHUCE6_SUB2API_API_KEY", "") else "password",
        )
        if sub2api_auth_mode == "api_key":
            sub2api_api_key = _prompt_text(
                input_fn,
                print_fn,
                "sub2api API Key",
                default=env_defaults.get("ZHUCE6_SUB2API_API_KEY", ""),
                required=True,
            )
        else:
            sub2api_admin_email = _prompt_text(
                input_fn,
                print_fn,
                "sub2api admin email",
                default=env_defaults.get("ZHUCE6_SUB2API_ADMIN_EMAIL", ""),
                required=True,
            )
            sub2api_admin_password = _prompt_text(
                input_fn,
                print_fn,
                "sub2api admin password",
                default=env_defaults.get("ZHUCE6_SUB2API_ADMIN_PASSWORD", ""),
                required=True,
            )
    else:
        _print_step(print_fn, 5, total_steps, "后端配置")
        print_fn("  lite 模式已跳过后端配置.")

    env_updates: dict[str, object] = {
        "ZHUCE6_RUN_MODE": mode,
        "ZHUCE6_HOST": host,
        "ZHUCE6_PORT": port,
        "ZHUCE6_DASHBOARD_PORT": port,
        "ZHUCE6_ENV_FILE": str(resolved_env_file),
        "ZHUCE6_CONFIG_DIR": str(config_dir),
        "ZHUCE6_BACKEND": backend,
        "ZHUCE6_REGISTER_MAIL_PROVIDER": register_mail_provider,
        "ZHUCE6_REGISTER_PROXY": register_proxy,
        "ZHUCE6_ENABLE_PROXY_POOL": "1" if enable_proxy_pool else "0",
        "ZHUCE6_PROXY_POOL_CONFIG": proxy_pool_config,
        "ZHUCE6_PROXY_POOL_DIRECT_URLS": proxy_pool_direct_urls,
    }

    if mode == "full" and backend == "cpa":
        env_updates.update(
            {
                "ZHUCE6_CPA_MANAGEMENT_BASE_URL": cpa_management_base_url,
                "ZHUCE6_CPA_MANAGEMENT_KEY": cpa_management_key,
            }
        )
    if mode == "full" and backend == "sub2api":
        env_updates.update(
            {
                "ZHUCE6_SUB2API_BASE_URL": sub2api_base_url,
                "ZHUCE6_SUB2API_API_KEY": sub2api_api_key,
                "ZHUCE6_SUB2API_ADMIN_EMAIL": sub2api_admin_email,
                "ZHUCE6_SUB2API_ADMIN_PASSWORD": sub2api_admin_password,
            }
        )

    print_fn("")
    print_fn("━" * 40)
    print_fn("配置摘要")
    print_fn("━" * 40)
    print_fn(f"  模式:      {mode}")
    if mode == "full":
        print_fn(f"  后端:      {backend}")
    if validation_proxy_url:
        print_fn(f"  代理:      {validation_proxy_url}")
    elif proxy_pool_config:
        print_fn(f"  代理:      Clash YAML -> {proxy_pool_config}")
    else:
        print_fn("  代理:      未设置")
    print_fn(f"  cfmail:    {email_domain or '未启用'}")
    print_fn(f"  Dashboard: http://{host}:{port}/zhuce6")

    should_save = _prompt_bool(input_fn, print_fn, f"保存到 {resolved_env_file}?", default=True)
    if should_save:
        if wrote_cfmail:
            if not reuse_existing_cfmail:
                runtime_config = setup_cfmail.prepare_runtime_cfmail_config(
                    api_token=cfmail_payload["cf_api_token"],
                    auth_email=cfmail_payload["cf_auth_email"],
                    auth_key=cfmail_payload["cf_auth_key"],
                    worker_domain=cfmail_payload["worker_domain"] or None,
                    zone_name=cfmail_payload["zone_name"],
                    worker_name=cfmail_payload["worker_name"],
                    mail_domain=cfmail_payload["email_domain"],
                    admin_password=cfmail_payload["admin_password"],
                    accounts_path=cfmail_accounts_path,
                    provision_env_path=cfmail_env_path,
                )
                cfmail_payload["worker_domain"] = runtime_config.worker_domain
                cfmail_payload["email_domain"] = runtime_config.email_domain
                cfmail_payload["admin_password"] = runtime_config.admin_password
                cfmail_payload["cf_account_id"] = runtime_config.account_id
                cfmail_payload["cf_zone_id"] = runtime_config.zone_id
            elif not cfmail_accounts_path.exists() or not cfmail_env_path.exists():
                setup_cfmail.write_cfmail_accounts_json(
                    cfmail_accounts_path,
                    worker_domain=cfmail_payload["worker_domain"],
                    email_domain=cfmail_payload["email_domain"],
                    worker_name=cfmail_payload["worker_name"],
                    admin_password=cfmail_payload["admin_password"],
                )
                setup_cfmail.write_cfmail_provision_env(
                    cfmail_env_path,
                    api_token=cfmail_payload["cf_api_token"],
                    auth_email=cfmail_payload["cf_auth_email"],
                    auth_key=cfmail_payload["cf_auth_key"],
                    account_id=cfmail_payload["cf_account_id"],
                    zone_id=cfmail_payload["cf_zone_id"],
                    worker_name=cfmail_payload["worker_name"],
                    zone_name=cfmail_payload["zone_name"],
                )
            env_updates.update(
                {
                    "ZHUCE6_CFMAIL_CONFIG_PATH": str(cfmail_accounts_path),
                    "ZHUCE6_CFMAIL_ENV_FILE": str(cfmail_env_path),
                    "ZHUCE6_D1_DATABASE_ID": getattr(runtime_config, "d1_database_id", "") if not reuse_existing_cfmail else d1_database_id,
                    "ZHUCE6_CFMAIL_API_TOKEN": cfmail_payload["cf_api_token"],
                    "ZHUCE6_CFMAIL_CF_AUTH_EMAIL": cfmail_payload["cf_auth_email"],
                    "ZHUCE6_CFMAIL_CF_AUTH_KEY": cfmail_payload["cf_auth_key"],
                    "ZHUCE6_CFMAIL_CF_ACCOUNT_ID": cfmail_payload["cf_account_id"],
                    "ZHUCE6_CFMAIL_CF_ZONE_ID": cfmail_payload["cf_zone_id"],
                    "ZHUCE6_CFMAIL_WORKER_NAME": cfmail_payload["worker_name"],
                    "ZHUCE6_CFMAIL_ZONE_NAME": cfmail_payload["zone_name"],
                }
            )
            print_fn("")
            print_fn("cfmail 运行时配置已保存.")
            if cfmail_payload["cf_api_token"]:
                print_fn("如果你还没有部署 cfmail Worker, 请在初始化完成后运行:")
                print_fn(
                    f"  uv run python scripts/setup_cfmail.py --api-token <token> --zone-name {cfmail_payload['zone_name']}"
                )
            else:
                print_fn("当前使用的是已部署 worker_domain, 初始化不会重新部署 cfmail Worker.")
            print_fn("")
        _persist_env_updates(resolved_env_file, env_updates)
        for key, value in env_updates.items():
            os.environ[str(key)] = str(value)
        print_fn(f"  ✅ 已保存到 {resolved_env_file}")
    else:
        if wrote_cfmail:
            wrote_cfmail = False
        print_fn("  已取消保存, 当前修改未写入磁盘.")

    print_fn("")
    print_fn("  下一步:")
    print_fn("    uv run python main.py doctor --fix   # 自动补齐依赖并检查环境")
    print_fn(f"    uv run python main.py --mode {mode}  # 启动")
    print_fn(f"初始化完成: {resolved_env_file}")
    if wrote_cfmail:
        print_fn(f"cfmail accounts: {cfmail_accounts_path}")
        print_fn(f"cfmail env: {cfmail_env_path}")

    return SetupWizardResult(
        env_file=resolved_env_file,
        env_updates=env_updates,
        cfmail_accounts_path=cfmail_accounts_path if wrote_cfmail else None,
        cfmail_env_path=cfmail_env_path if wrote_cfmail else None,
    )

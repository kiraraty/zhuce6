"""Environment doctor checks for zhuce6."""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Iterable
from urllib.parse import urlparse

from core.cfmail import enabled_cfmail_accounts
from core.settings import AppSettings
from dashboard.api import _cpa_dependency_payload, _sub2api_dependency_payload


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    summary: str
    detail: str = ""
    required_for: tuple[str, ...] = ("lite", "full")


@dataclass(frozen=True)
class DoctorReport:
    settings: AppSettings
    checks: tuple[DoctorCheck, ...]
    lite_available: bool
    full_available: bool
    full_cpa_available: bool
    full_sub2api_available: bool


def sslocal_install_guidance() -> str:
    return "\n".join(
        [
            "如果你需要 SS 节点代理池, 请安装 shadowsocks-rust:",
            "",
            "Linux:",
            "Linux (Debian/Ubuntu):",
            "  curl -fsSL https://github.com/shadowsocks/shadowsocks-rust/releases/latest/download/shadowsocks-v*-x86_64-unknown-linux-gnu.tar.xz | tar -xJ -C /usr/local/bin sslocal",
            "",
            "macOS:",
            "  brew install shadowsocks-rust",
            "",
            "Windows:",
            "  下载: https://github.com/shadowsocks/shadowsocks-rust/releases/latest",
            "  选择 shadowsocks-*-x86_64-pc-windows-msvc.zip, 解压 sslocal.exe 到 PATH",
            "",
            "如果你已有代理 (Clash/V2Ray), 可以跳过安装:",
            "  在 .env 中设置: ZHUCE6_PROXY_POOL_DIRECT_URLS=socks5://127.0.0.1:7891",
        ]
    )


def _project_root(settings: AppSettings | None = None) -> Path:
    return (settings.project_root if settings is not None else AppSettings.from_env().project_root).resolve()


def apply_doctor_fixes(settings: AppSettings | None = None) -> list[str]:
    active_settings = settings or AppSettings.from_env()
    repo_root = _project_root(active_settings)
    actions: list[str] = []
    subprocess.run(["uv", "sync"], cwd=str(repo_root), check=True)
    actions.append(f"uv sync @ {repo_root}")
    worker_dir = repo_root / "vendor" / "cfmail-worker" / "worker"
    if (worker_dir / "package.json").is_file():
        subprocess.run(["npm", "install", "--no-fund", "--no-audit"], cwd=str(worker_dir), check=True)
        actions.append(f"npm install @ {worker_dir}")
    return actions


def _check_python_version(_settings: AppSettings) -> DoctorCheck:
    current = sys.version_info
    required = (3, 11)
    if current >= required:
        return DoctorCheck(
            name="python",
            status="ok",
            summary=f"Python {current.major}.{current.minor}.{current.micro} 满足 >= 3.11",
        )
    return DoctorCheck(
        name="python",
        status="error",
        summary=f"Python {current.major}.{current.minor}.{current.micro} 低于 >= 3.11",
    )


def _check_env_file(settings: AppSettings) -> DoctorCheck:
    if not settings.env_file.exists():
        return DoctorCheck("env", "error", f".env 不存在: {settings.env_file}")
    try:
        settings.env_file.read_text(encoding="utf-8")
    except OSError as exc:
        return DoctorCheck("env", "error", f".env 无法读取: {exc}")
    return DoctorCheck("env", "ok", f".env 可读取: {settings.env_file}")


def _check_core_dependencies(_settings: AppSettings) -> DoctorCheck:
    modules = {
        "fastapi": "fastapi",
        "uvicorn": "uvicorn",
        "PyYAML": "yaml",
        "httpx": "httpx",
        "curl_cffi": "curl_cffi",
        "sqlmodel": "sqlmodel",
        "cbor2": "cbor2",
        "jwcrypto": "jwcrypto",
        "filelock": "filelock",
        "psutil": "psutil",
        "socksio": "socksio",
    }
    missing: list[str] = []
    for display_name, module_name in modules.items():
        try:
            importlib.import_module(module_name)
        except ModuleNotFoundError:
            missing.append(display_name)
    if missing:
        return DoctorCheck("deps", "error", f"缺少核心依赖: {', '.join(missing)}")
    return DoctorCheck("deps", "ok", "核心依赖齐全")


def _check_cfmail(settings: AppSettings) -> DoctorCheck:
    providers = {part.strip().lower() for part in settings.register_mail_provider.split(",") if part.strip()}
    if "cfmail" not in providers:
        return DoctorCheck("cfmail", "skip", "register 未启用 cfmail", required_for=())
    missing = settings.validate_cfmail_env()
    if missing:
        return DoctorCheck("cfmail", "error", f"cfmail 缺少环境变量: {', '.join(missing)}")
    configured_path = Path(
        str(os.getenv("ZHUCE6_CFMAIL_CONFIG_PATH", str(settings.config_dir / "cfmail_accounts.json")))
    ).expanduser().resolve()
    accounts = enabled_cfmail_accounts(configured_path)
    if not accounts:
        return DoctorCheck("cfmail", "error", f"cfmail 账号配置为空: {configured_path}")
    active = accounts[0]
    return DoctorCheck(
        "cfmail",
        "ok",
        f"cfmail 已配置: {active.name} -> {active.email_domain}",
        detail=str(configured_path),
    )


def _check_proxy(settings: AppSettings) -> DoctorCheck:
    direct_proxy = str(settings.register_proxy or "").strip()
    direct_urls = str(settings.proxy_pool_direct_urls or "").strip()
    config_path = settings.proxy_pool_config
    socks_proxies: list[str] = []
    if direct_proxy and _is_socks_proxy_url(direct_proxy):
        socks_proxies.append(direct_proxy)
    if direct_urls:
        socks_proxies.extend(
            [item.strip() for item in direct_urls.split(";") if item.strip() and _is_socks_proxy_url(item.strip())]
        )
    if socks_proxies and not _has_socksio():
        return DoctorCheck(
            "proxy",
            "error",
            "已配置 SOCKS 代理, 但缺少 SOCKS 支持依赖",
            detail=f"缺少 Python 包: socksio\n请先运行: uv sync\n检测到的 SOCKS 代理: {', '.join(socks_proxies)}",
        )
    if direct_proxy:
        return DoctorCheck("proxy", "ok", f"register 代理已配置: {direct_proxy}")
    if direct_urls:
        count = len([item for item in direct_urls.split(";") if item.strip()])
        return DoctorCheck("proxy", "ok", f"direct proxy URLs 已配置: {count} 条")
    if config_path:
        if not Path(config_path).exists():
            return DoctorCheck("proxy", "error", f"代理池配置不存在: {config_path}")
        return DoctorCheck("proxy", "ok", f"代理池配置存在: {config_path}")
    return DoctorCheck("proxy", "error", "未配置 register_proxy, direct proxy URLs 或 proxy pool config")


def _is_socks_proxy_url(proxy_url: str) -> bool:
    scheme = urlparse(str(proxy_url or "").strip()).scheme.lower()
    return scheme.startswith("socks")


def _has_socksio() -> bool:
    try:
        importlib.import_module("socksio")
    except ModuleNotFoundError:
        return False
    return True


def _touch_directory(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".doctor-", dir=path, delete=True):
            pass
    except OSError as exc:
        return False, str(exc)
    return True, "ok"


def _check_directory_writable(settings: AppSettings) -> DoctorCheck:
    targets: list[Path] = [
        settings.config_dir,
        settings.state_dir,
        settings.log_dir,
        settings.pool_dir,
        settings.env_file.parent,
    ]
    failures: list[str] = []
    for directory in targets:
        ok, detail = _touch_directory(directory)
        if not ok:
            failures.append(f"{directory}: {detail}")
    if failures:
        return DoctorCheck("dirs", "error", "目录不可写", detail="; ".join(failures))
    return DoctorCheck("dirs", "ok", "核心目录可写")


def _check_sslocal(settings: AppSettings) -> DoctorCheck:
    if settings.proxy_pool_direct_urls.strip():
        return DoctorCheck("sslocal", "skip", "使用 direct proxy URLs, 不依赖 sslocal", required_for=())
    if not settings.proxy_pool_config:
        return DoctorCheck("sslocal", "skip", "未启用基于配置文件的代理池", required_for=())
    sslocal_bin = shutil.which("sslocal") or shutil.which("ss-local")
    if sslocal_bin:
        return DoctorCheck("sslocal", "ok", f"sslocal 可用: {sslocal_bin}")
    return DoctorCheck(
        "sslocal",
        "error",
        "未安装 sslocal",
        detail=sslocal_install_guidance(),
    )


def _check_cpa_management(settings: AppSettings) -> DoctorCheck:
    payload = _cpa_dependency_payload(settings)
    if settings.runtime_mode == "lite":
        return DoctorCheck("cpa", "skip", "lite 模式不检查 CPA", required_for=())
    if settings.backend != "cpa":
        return DoctorCheck("cpa", "skip", "当前 backend 不是 cpa", required_for=())
    if bool(payload.get("management_reachable")):
        return DoctorCheck("cpa", "ok", "CPA management 可达", required_for=("full",))
    return DoctorCheck(
        "cpa",
        "error",
        "CPA management 不可达",
        detail=f"management_reachable={payload.get('management_reachable', False)}",
        required_for=("full",),
    )


def _check_sub2api(settings: AppSettings) -> DoctorCheck:
    payload = _sub2api_dependency_payload(settings)
    if settings.runtime_mode == "lite":
        return DoctorCheck("sub2api", "skip", "lite 模式不检查 sub2api", required_for=())
    if settings.backend != "sub2api":
        return DoctorCheck("sub2api", "skip", "当前 backend 不是 sub2api", required_for=())
    if payload.get("status") == "ok":
        return DoctorCheck("sub2api", "ok", "sub2api 可达", detail=str(payload.get("base_url") or settings.sub2api_base_url), required_for=("full",))
    error = str(payload.get("error") or "unreachable")
    auth_configured = bool(payload.get("auth_configured"))
    return DoctorCheck(
        "sub2api",
        "error",
        f"sub2api 不可用: {error}",
        detail=f"base_url={settings.sub2api_base_url}\nauth_configured={auth_configured}",
        required_for=("full",),
    )


def _is_lite_available(checks: Iterable[DoctorCheck]) -> bool:
    relevant_names = {"python", "env", "deps", "cfmail", "proxy", "dirs", "sslocal"}
    relevant = [check for check in checks if check.name in relevant_names and check.status != "skip"]
    return all(check.status == "ok" for check in relevant)


def _is_full_cpa_available(checks: Iterable[DoctorCheck]) -> bool:
    if not _is_lite_available(checks):
        return False
    relevant = [check for check in checks if check.name == "cpa" and check.status != "skip"]
    return all(check.status == "ok" for check in relevant) and bool(relevant)


def _is_full_sub2api_available(checks: Iterable[DoctorCheck]) -> bool:
    if not _is_lite_available(checks):
        return False
    relevant = [check for check in checks if check.name == "sub2api" and check.status != "skip"]
    return all(check.status == "ok" for check in relevant) and bool(relevant)


def collect_doctor_report(settings: AppSettings | None = None) -> DoctorReport:
    active_settings = settings or AppSettings.from_env()
    checks = (
        _check_python_version(active_settings),
        _check_env_file(active_settings),
        _check_core_dependencies(active_settings),
        _check_cfmail(active_settings),
        _check_proxy(active_settings),
        _check_directory_writable(active_settings),
        _check_sslocal(active_settings),
        _check_cpa_management(active_settings),
        _check_sub2api(active_settings),
    )
    lite_available = _is_lite_available(checks)
    full_cpa_available = _is_full_cpa_available(checks)
    full_sub2api_available = _is_full_sub2api_available(checks)
    return DoctorReport(
        settings=active_settings,
        checks=checks,
        lite_available=lite_available,
        full_available=full_cpa_available if active_settings.backend == "cpa" else full_sub2api_available if active_settings.backend == "sub2api" else False,
        full_cpa_available=full_cpa_available,
        full_sub2api_available=full_sub2api_available,
    )


def format_doctor_report(report: DoctorReport) -> str:
    lines = [
        "zhuce6 doctor",
        f"env_file: {report.settings.env_file}",
        "",
    ]
    for check in report.checks:
        lines.append(f"- {check.name:<8} {check.status:<5} {check.summary}")
        if check.detail:
            for detail_line in str(check.detail).splitlines():
                lines.append(f"  {detail_line}" if detail_line else "")
    lines.extend(
        [
            "",
            "conclusion:",
            f"- lite: {'available' if report.lite_available else 'unavailable'}",
            f"- full: {'available' if report.full_available else 'unavailable'}",
            f"- full(cpa): {'available' if report.full_cpa_available else 'unavailable'}",
            f"- full(sub2api): {'available' if report.full_sub2api_available else 'unavailable'}",
        ]
    )
    return "\n".join(lines)

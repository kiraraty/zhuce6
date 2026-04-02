"""zhuce6 unified entrypoint."""

from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any

from core.env_loader import bootstrap_env, load_env_file as _load_env_file

bootstrap_env(Path(__file__).resolve().parent)

try:
    from fastapi import FastAPI, HTTPException, Request, Response
    from fastapi.responses import HTMLResponse
    from pydantic import BaseModel, Field
    import uvicorn
    WEB_RUNTIME_IMPORT_ERROR: ModuleNotFoundError | None = None
except ModuleNotFoundError as exc:
    WEB_RUNTIME_IMPORT_ERROR = exc
    FastAPI = Any  # type: ignore[assignment]
    Request = Any  # type: ignore[assignment]
    Response = Any  # type: ignore[assignment]
    HTMLResponse = Any  # type: ignore[assignment]

    class HTTPException(Exception):
        def __init__(self, status_code: int, detail: str = "") -> None:
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class BaseModel:  # type: ignore[no-redef]
        pass

    def Field(*, default=None, alias=None):  # type: ignore[no-redef]
        return default

    uvicorn = None  # type: ignore[assignment]

try:
    from core import process_manager
    from core.chatgpt_flow_runner import (
        run_chatgpt_callback_exchange,
        run_chatgpt_preflight,
        run_chatgpt_register_once,
    )
    from core.doctor import collect_doctor_report, format_doctor_report
    from core.paths import DEFAULT_DASHBOARD_LOG_FILE
    from core.registration import RegistrationBurstScheduler, RegistrationLoop
    from core.registry import list_platforms, load_all
    from core.settings import AppSettings
    from core.setup_wizard import run_setup_wizard
    from dashboard.api import (
        _account_survival_payload,
        _build_background_tasks,
        _cpa_dependency_payload,
        _cfmail_dependency_payload,
        _count_cpa_files,
        _count_today_new,
        _fetch_management_auth_files,
        _parse_settings_patch,
        _persist_env_updates,
        _proxy_pool_dependency_payload,
        _recent_pool_files,
        _runtime_payload,
        _settings_payload,
        _sub2api_dependency_payload,
        _summary_payload,
    )
    from ops.rotate_log import _rotate_log_tail
    APP_IMPORT_ERROR: ModuleNotFoundError | None = None
except ModuleNotFoundError as exc:
    process_manager = Any  # type: ignore[assignment]
    run_chatgpt_callback_exchange = Any  # type: ignore[assignment]
    run_chatgpt_preflight = Any  # type: ignore[assignment]
    run_chatgpt_register_once = Any  # type: ignore[assignment]
    collect_doctor_report = Any  # type: ignore[assignment]
    format_doctor_report = Any  # type: ignore[assignment]
    DEFAULT_DASHBOARD_LOG_FILE = Path("dashboard.log")
    RegistrationBurstScheduler = Any  # type: ignore[assignment]
    RegistrationLoop = Any  # type: ignore[assignment]
    list_platforms = Any  # type: ignore[assignment]
    load_all = Any  # type: ignore[assignment]
    AppSettings = Any  # type: ignore[assignment]
    run_setup_wizard = Any  # type: ignore[assignment]
    _account_survival_payload = Any  # type: ignore[assignment]
    _build_background_tasks = Any  # type: ignore[assignment]
    _cpa_dependency_payload = Any  # type: ignore[assignment]
    _cfmail_dependency_payload = Any  # type: ignore[assignment]
    _count_cpa_files = Any  # type: ignore[assignment]
    _count_today_new = Any  # type: ignore[assignment]
    _fetch_management_auth_files = Any  # type: ignore[assignment]
    _parse_settings_patch = Any  # type: ignore[assignment]
    _persist_env_updates = Any  # type: ignore[assignment]
    _proxy_pool_dependency_payload = Any  # type: ignore[assignment]
    _recent_pool_files = Any  # type: ignore[assignment]
    _runtime_payload = Any  # type: ignore[assignment]
    _settings_payload = Any  # type: ignore[assignment]
    _sub2api_dependency_payload = Any  # type: ignore[assignment]
    _summary_payload = Any  # type: ignore[assignment]
    _rotate_log_tail = Any  # type: ignore[assignment]
    APP_IMPORT_ERROR = exc

DASHBOARD_MODES = {"full", "dashboard", "lite"}
WORKER_MODES = {"register-loop", "burst-scheduler"}
ALL_RUNTIME_MODES = DASHBOARD_MODES | WORKER_MODES
DASHBOARD_CORS_PATHS = frozenset({"/api/runtime", "/api/summary"})


class _ValidateOpsProxy:
    def __getattr__(self, name: str) -> object:
        from ops import validate as validate_module

        return getattr(validate_module, name)


validate_ops = _ValidateOpsProxy()


def classify_token_file(*args, **kwargs):  # type: ignore[no-untyped-def]
    from ops.scan import classify_token_file as _classify_token_file

    return _classify_token_file(*args, **kwargs)


def _ensure_web_runtime_available() -> None:
    if WEB_RUNTIME_IMPORT_ERROR is not None or uvicorn is None:
        raise ModuleNotFoundError(
            "Web runtime dependencies are unavailable. Run `uv sync` or use `uv run python main.py ...`."
        ) from WEB_RUNTIME_IMPORT_ERROR


def _ensure_app_dependencies_available() -> None:
    if APP_IMPORT_ERROR is not None:
        raise ModuleNotFoundError(
            f"Missing dependency: {APP_IMPORT_ERROR.name or APP_IMPORT_ERROR}. Run `uv sync` first."
        ) from APP_IMPORT_ERROR


def _handle_missing_dependency_import() -> None:
    exc = APP_IMPORT_ERROR or WEB_RUNTIME_IMPORT_ERROR
    missing = exc.name if isinstance(exc, ModuleNotFoundError) else "dependency"
    print(
        "缺少运行依赖, 当前命令无法继续.\n"
        f"missing: {missing}\n"
        "请先执行: uv sync",
        file=sys.stderr,
    )
    raise SystemExit(1)


def _run_uv_sync() -> bool:
    result = subprocess.run(
        ["uv", "sync"],
        cwd=Path(__file__).resolve().parent,
        check=False,
        text=True,
    )
    return result.returncode == 0


class ChatGPTPreflightRequest(BaseModel):
    email: str | None = None
    password: str | None = None
    proxy: str | None = None
    mail_provider: str = Field(default="cfmail")


class ChatGPTCallbackExchangeRequest(BaseModel):
    callback_url: str
    expected_state: str = Field(alias="state")
    code_verifier: str
    proxy: str | None = None
    write_pool: bool = True


class ChatGPTRegisterRequest(BaseModel):
    email: str | None = None
    password: str | None = None
    proxy: str | None = None
    mail_provider: str = Field(default="cfmail")
    write_pool: bool = True


class RegisterControlRequest(BaseModel):
    action: str


def _apply_runtime_mode(settings: AppSettings, mode: str) -> AppSettings:
    normalized = mode if mode in ALL_RUNTIME_MODES else "full"
    updated = replace(settings, runtime_mode=normalized)
    if normalized == "dashboard":
        updated = replace(updated, register_enabled=False)
    elif normalized in {"full", "lite", "register-loop", "burst-scheduler"}:
        updated = replace(updated, register_enabled=True)
    if normalized == "lite":
        updated = replace(
            updated,
            cleanup_enabled=False,
            d1_cleanup_enabled=False,
            validate_enabled=False,
            rotate_enabled=False,
            account_survival_enabled=False,
        )
    return updated


def _dashboard_cors_headers(origin: str) -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Accept, Content-Type",
        "Access-Control-Max-Age": "600",
        "Vary": "Origin",
    }


def create_app(enable_background_tasks: bool = True, mode: str = "full") -> FastAPI:
    _ensure_app_dependencies_available()
    _ensure_web_runtime_available()
    settings = _apply_runtime_mode(AppSettings.from_env(), mode)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        load_all()
        app.state.settings = settings
        app.state.dashboard_overview_cache = None
        app.state.background_tasks = _build_background_tasks(settings) if enable_background_tasks else []
        app.state.registration_loop = None
        for task in app.state.background_tasks:
            task.start()
        if settings.register_enabled and settings.runtime_mode in {"full", "lite"}:
            reg_loop = RegistrationLoop(settings)
            reg_loop.start()
            app.state.registration_loop = reg_loop
        try:
            yield
        finally:
            if app.state.registration_loop:
                app.state.registration_loop.stop()
            for task in app.state.background_tasks:
                task.stop()

    app = FastAPI(title="zhuce6", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def dashboard_cors_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        origin = request.headers.get("origin", "").strip().rstrip("/")
        allowed_origins = {
            str(item or "").strip().rstrip("/")
            for item in getattr(settings, "dashboard_allowed_origins", ())
            if str(item or "").strip()
        }
        if request.url.path not in DASHBOARD_CORS_PATHS or not origin or origin not in allowed_origins:
            return await call_next(request)
        if request.method == "OPTIONS":
            return Response(status_code=204, headers=_dashboard_cors_headers(origin))
        response = await call_next(request)
        for key, value in _dashboard_cors_headers(origin).items():
            response.headers[key] = value
        return response

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/platforms")
    def api_platforms() -> list[dict[str, str]]:
        return list_platforms()

    @app.get("/api/runtime")
    def api_runtime() -> dict[str, object]:
        return _runtime_payload(app)

    @app.get("/api/summary")
    def api_summary() -> dict[str, object]:
        return _summary_payload(app)

    @app.get("/api/account-survival")
    def api_account_survival() -> dict[str, object]:
        return _account_survival_payload(app.state.settings)

    @app.get("/api/settings")
    def api_settings() -> dict[str, object]:
        return _settings_payload(app)

    @app.put("/api/settings")
    async def api_settings_update(request: Request) -> dict[str, object]:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="settings patch must be an object")
        updates, env_updates = _parse_settings_patch(payload)
        app.state.settings = replace(app.state.settings, **updates)
        app.state.dashboard_overview_cache = None
        env_file_path = Path(
            str(os.getenv("ZHUCE6_ENV_FILE", str(app.state.settings.env_file))).strip()
            or str(app.state.settings.env_file)
        ).expanduser().resolve()
        _persist_env_updates(env_file_path, env_updates)
        app.state.settings = replace(app.state.settings, env_file=env_file_path)
        response = _settings_payload(app)
        response["restart_required"] = bool(env_updates)
        return response

    @app.post("/api/control/register")
    async def api_control_register(request: RegisterControlRequest) -> dict[str, object]:
        action = str(request.action or "").strip().lower()
        if action not in {"start", "stop", "restart"}:
            raise HTTPException(status_code=400, detail=f"unsupported action: {action}")

        active_loop = getattr(app.state, "registration_loop", None)
        if action in {"stop", "restart"} and active_loop is not None:
            active_loop.stop()
            app.state.registration_loop = None
            app.state.settings = replace(app.state.settings, register_enabled=False)
        if action in {"start", "restart"}:
            next_settings = replace(app.state.settings, register_enabled=True)
            reg_loop = RegistrationLoop(next_settings)
            reg_loop.start()
            app.state.settings = next_settings
            app.state.registration_loop = reg_loop
        status_map = {"start": "started", "stop": "stopped", "restart": "restarted"}
        return {"status": status_map[action], "register_enabled": bool(app.state.registration_loop is not None)}

    @app.get("/api/health/dependencies")
    def api_health_dependencies() -> dict[str, object]:
        settings = app.state.settings
        return {
            "cfmail": _cfmail_dependency_payload(settings),
            "proxy_pool": _proxy_pool_dependency_payload(app),
            "cpa": _cpa_dependency_payload(settings),
            "sub2api": _sub2api_dependency_payload(settings),
        }

    @app.post("/api/account-survival/reseed")
    def api_account_survival_reseed() -> dict[str, object]:
        from ops.account_survival import account_survival_once

        payload = account_survival_once(
            pool_dir=app.state.settings.pool_dir,
            state_file=app.state.settings.account_survival_state_file,
            cohort_size=app.state.settings.account_survival_cohort_size,
            proxy=app.state.settings.account_survival_proxy,
            timeout_seconds=app.state.settings.account_survival_timeout_seconds,
            reseed=True,
        )
        payload["enabled"] = app.state.settings.account_survival_enabled
        payload["available"] = True
        payload["path"] = str(app.state.settings.account_survival_state_file)
        return payload

    @app.get("/api/platforms/chatgpt/actions")
    def api_chatgpt_actions() -> list[dict[str, Any]]:
        from core.registry import get

        platform_cls = get("chatgpt")
        platform = platform_cls()
        return platform.get_platform_actions()

    @app.post("/api/register/chatgpt/preflight")
    def api_chatgpt_preflight(request: ChatGPTPreflightRequest) -> dict[str, object]:
        return run_chatgpt_preflight(
            email=request.email,
            password=request.password,
            mail_provider=request.mail_provider,
            proxy=request.proxy,
        )

    @app.post("/api/register/chatgpt/run")
    def api_chatgpt_register_once(request: ChatGPTRegisterRequest) -> dict[str, object]:
        return run_chatgpt_register_once(
            email=request.email,
            password=request.password,
            mail_provider=request.mail_provider,
            proxy=request.proxy,
            write_pool=request.write_pool,
            pool_dir=app.state.settings.pool_dir,
        )

    @app.post("/api/register/chatgpt/callback-exchange")
    def api_chatgpt_callback_exchange(request: ChatGPTCallbackExchangeRequest) -> dict[str, object]:
        return run_chatgpt_callback_exchange(
            callback_url=request.callback_url,
            expected_state=request.expected_state,
            code_verifier=request.code_verifier,
            proxy=request.proxy,
            write_pool=request.write_pool,
            pool_dir=app.state.settings.pool_dir,
        )

    @app.get("/zhuce6", response_class=HTMLResponse)
    def zhuce6_page() -> str:
        html_path = Path(__file__).parent / "dashboard" / "zhuce6.html"
        if html_path.is_file():
            return html_path.read_text(encoding="utf-8")
        return "<h1>zhuce6.html not found</h1>"

    return app


app = create_app() if WEB_RUNTIME_IMPORT_ERROR is None and APP_IMPORT_ERROR is None else None


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start the zhuce6 service runtime")
    parser.add_argument(
        "command",
        nargs="?",
        choices=("run", "stop", "status", "init", "doctor"),
        default="run",
        help="Lifecycle command",
    )
    parser.add_argument("--mode", choices=sorted(ALL_RUNTIME_MODES), default="full", help="Runtime mode")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8000, help="Bind port")
    parser.add_argument("--reload", action="store_true", help="Enable reload mode for development")
    parser.add_argument("--no-background-tasks", action="store_true", help="Disable internal background loops")
    parser.add_argument("--register-loop", action="store_true", help="Enable continuous registration loop")
    parser.add_argument("--register-loop-only", action="store_true", help="Run only the continuous registration loop")
    parser.add_argument(
        "--register-burst-scheduler-only",
        action="store_true",
        help="Run only the burst registration scheduler",
    )
    parser.add_argument("--register-threads", type=int, default=None, help="Number of registration threads")
    parser.add_argument("--target-count", type=int, default=None, help="Stop registration after N successes")
    parser.add_argument("--batch-threads", type=int, default=None, help="Number of threads per burst batch")
    parser.add_argument("--batch-target-count", type=int, default=None, help="Target successes per burst batch")
    parser.add_argument("--batch-interval-seconds", type=int, default=None, help="Seconds between burst starts")
    parser.add_argument("--fix", action="store_true", help="For doctor/init: run `uv sync` before re-checking")
    return parser


def _warn_deprecated_flag(flag: str, replacement: str) -> None:
    print(f"warning: {flag} is deprecated; use {replacement} instead.", file=sys.stderr)


def _resolve_mode(args: argparse.Namespace) -> str:
    mode = str(args.mode or "full")
    if args.register_burst_scheduler_only:
        _warn_deprecated_flag("--register-burst-scheduler-only", "--mode burst-scheduler")
        return "burst-scheduler"
    if args.register_loop_only:
        _warn_deprecated_flag("--register-loop-only", "--mode register-loop")
        return "register-loop"
    if args.register_loop:
        _warn_deprecated_flag("--register-loop", "--mode full")
        return "full"
    return mode


def _apply_cli_env_overrides(args: argparse.Namespace, mode: str) -> None:
    os.environ["ZHUCE6_RUNTIME_MODE"] = mode
    os.environ["ZHUCE6_HOST"] = str(args.host)
    os.environ["ZHUCE6_PORT"] = str(args.port)
    os.environ["ZHUCE6_DASHBOARD_PORT"] = str(args.port)
    os.environ["ZHUCE6_REGISTER_ENABLED"] = "true" if mode in {"full", "lite", "register-loop", "burst-scheduler"} else "false"
    if args.register_threads is not None:
        os.environ["ZHUCE6_REGISTER_THREADS"] = str(args.register_threads)
    if args.target_count is not None:
        os.environ["ZHUCE6_REGISTER_TARGET_COUNT"] = str(args.target_count)
    if args.batch_threads is not None:
        os.environ["ZHUCE6_REGISTER_BATCH_THREADS"] = str(args.batch_threads)
    if args.batch_target_count is not None:
        os.environ["ZHUCE6_REGISTER_BATCH_TARGET_COUNT"] = str(args.batch_target_count)
    if args.batch_interval_seconds is not None:
        os.environ["ZHUCE6_REGISTER_BATCH_INTERVAL_SECONDS"] = str(args.batch_interval_seconds)


def _pid_name_for_mode(mode: str) -> str:
    return mode if mode in {"register-loop", "burst-scheduler"} else "main"


def _ensure_runtime_cfmail_env(settings: AppSettings, mode: str) -> None:
    if mode not in {"full", "lite", "register-loop", "burst-scheduler"}:
        return
    providers = {part.strip() for part in settings.register_mail_provider.split(",") if part.strip()}
    if "cfmail" not in providers:
        return
    missing = settings.validate_cfmail_env()
    if missing:
        raise SystemExit(
            "Missing cfmail provisioning env: "
            + " ".join(missing)
            + f"\nExpected env file: {os.getenv('ZHUCE6_CFMAIL_ENV_FILE', str(settings.config_dir / 'cfmail_provision.env'))}"
        )


def main(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    if args.command == "stop":
        _ensure_app_dependencies_available()
        print(json.dumps({"stopped": process_manager.stop_all()}, ensure_ascii=False, indent=2))
        return
    if args.command == "status":
        _ensure_app_dependencies_available()
        print(json.dumps({"processes": process_manager.status_all()}, ensure_ascii=False, indent=2))
        return
    if args.command == "init":
        if APP_IMPORT_ERROR is not None:
            _handle_missing_dependency_import()
        run_setup_wizard()
        _run_uv_sync()
        return
    if args.command == "doctor":
        if APP_IMPORT_ERROR is not None:
            _handle_missing_dependency_import()
        if args.fix:
            _run_uv_sync()
        print(format_doctor_report(collect_doctor_report()))
        return

    mode = _resolve_mode(args)
    _apply_cli_env_overrides(args, mode)
    settings = _apply_runtime_mode(AppSettings.from_env(), mode)
    _ensure_runtime_cfmail_env(settings, mode)
    process_manager.stop_all()

    pid_name = _pid_name_for_mode(mode)
    process_manager.write_pid(pid_name)
    try:
        if mode == "register-loop":
            reg_loop = RegistrationLoop(settings)
            reg_loop.start()
            try:
                while True:
                    time.sleep(5)
            except KeyboardInterrupt:
                pass
            finally:
                reg_loop.stop()
            return
        if mode == "burst-scheduler":
            scheduler = RegistrationBurstScheduler(settings)
            try:
                scheduler.run()
            except KeyboardInterrupt:
                pass
            finally:
                scheduler.stop()
            return
        create_mode = mode if mode in DASHBOARD_MODES else "full"
        uvicorn.run(
            create_app(enable_background_tasks=not args.no_background_tasks, mode=create_mode),
            host=args.host,
            port=args.port,
            reload=args.reload,
        )
    finally:
        process_manager.remove_pid(pid_name)


if __name__ == "__main__":
    main()

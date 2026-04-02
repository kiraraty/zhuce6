import asyncio
import json
from pathlib import Path

from core.settings import AppSettings
from main import _build_background_tasks, _recent_pool_files, _rotate_log_tail, _runtime_payload, _summary_payload, create_app
from ops.scan import ScanResult


def _request_via_asgi(
    app,
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> tuple[int, dict[str, str], bytes]:
    request_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": request_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "root_path": "",
        "app": app,
    }
    response: dict[str, object] = {"status": 500, "headers": {}, "body": b""}
    request_sent = False

    async def receive() -> dict[str, object]:
        nonlocal request_sent
        if request_sent:
            return {"type": "http.disconnect"}
        request_sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.start":
            response["status"] = int(message["status"])
            response["headers"] = {
                key.decode("latin-1"): value.decode("latin-1")
                for key, value in message.get("headers", [])
            }
            return
        if message["type"] == "http.response.body":
            response["body"] = bytes(response["body"]) + bytes(message.get("body", b""))

    asyncio.run(app(scope, receive, send))
    return int(response["status"]), dict(response["headers"]), bytes(response["body"])


def test_summary_payload_exposes_register_and_ops_commands(tmp_path: Path) -> None:
    app = create_app(enable_background_tasks=False)
    app.state.settings = AppSettings(
        pool_dir=tmp_path,
        runtime_state_file=tmp_path / "runtime_state.json",
        cleanup_enabled=False,
        validate_enabled=False,
    )
    app.state.background_tasks = []
    payload = _summary_payload(app)
    commands = payload["commands"]
    routes = payload["routes"]
    assert commands["chatgpt_preflight"].startswith("uv run python scripts/chatgpt_preflight.py")
    assert commands["chatgpt_register_once"].startswith("uv run python scripts/chatgpt_register_once.py")
    assert commands["chatgpt_callback_exchange"].startswith("uv run python scripts/chatgpt_exchange_callback.py")
    assert commands["update_priority_dry_run"].startswith("uv run python -m ops.update_priority")
    assert commands["validate_used_dry_run"].startswith("uv run python -m ops.validate --scope used")
    assert routes["chatgpt_callback_exchange"] == "/api/register/chatgpt/callback-exchange"
    assert "dashboard" not in routes


def test_dashboard_route_removed(tmp_path: Path) -> None:
    app = create_app(enable_background_tasks=False)
    app.state.settings = AppSettings(
        pool_dir=tmp_path,
        runtime_state_file=tmp_path / "runtime_state.json",
        cleanup_enabled=False,
        validate_enabled=False,
    )
    app.state.background_tasks = []

    status, _headers, _body = _request_via_asgi(app, "GET", "/dashboard")

    assert status == 404


def test_summary_payload_exposes_register_log_tail(tmp_path: Path) -> None:
    log_path = tmp_path / "register.log"
    log_path.write_text("line-1\nline-2\nline-3\n", encoding="utf-8")

    app = create_app(enable_background_tasks=False)
    app.state.settings = AppSettings(
        pool_dir=tmp_path,
        register_log_file=str(log_path),
        cleanup_enabled=False,
        validate_enabled=False,
    )
    app.state.background_tasks = []

    payload = _summary_payload(app)
    log_tail = payload["register_log_tail"]

    assert log_tail["available"] is True
    assert log_tail["path"] == str(log_path)
    assert log_tail["error"] is None
    assert log_tail["lines"] == ["line-1", "line-2", "line-3"]


def test_summary_payload_exposes_rotate_log_tail_and_runtime_state_meta(monkeypatch, tmp_path: Path) -> None:
    runtime_state_file = tmp_path / "runtime_state.json"
    runtime_state_file.write_text('{"updated_at":"2026-03-23T22:25:02"}', encoding="utf-8")

    app = create_app(enable_background_tasks=False)
    app.state.settings = AppSettings(
        pool_dir=tmp_path,
        runtime_state_file=runtime_state_file,
        cleanup_enabled=False,
        validate_enabled=False,
    )
    app.state.background_tasks = []

    monkeypatch.setattr(
        "main._rotate_log_tail",
        lambda **_kwargs: {
            "available": True,
            "path": "/home/sophomores/zhuce6/logs/dashboard.log",
            "updated_at": 1774276100.0,
            "updated_at_iso": "2026-03-23T22:28:20",
            "error": None,
            "lines": [
                "[22:18:20] [rotate] summary | 主池: 800 → 790 | 401删除: 10 | quota探测: 797 | probe401: 14 | probe429: 0 | probe跳过: 0 | 429删除: 0",
            ],
            "recent_events": [
                "[22:17:35] [rotate] 🔎 ocd0553a1fb1@mail.example.test.json quota probe → 401 invalidated",
                "[22:17:35] [rotate] ❌ ocd0553a1fb1@mail.example.test.json 401删除",
            ],
            "latest_summary": {
                "time": "22:18:20",
                "main_before": 800,
                "main_after": 790,
                "deleted_401": 10,
                "quota_probed": 797,
                "quota_probe_401": 14,
                "quota_probe_429": 0,
                "quota_probe_skipped": 0,
                "deleted_429": 0,
            },
            "current_summary": {
                "time": "22:19:10",
                "main_before": None,
                "main_after": None,
                "deleted_401": 1,
                "quota_probed": 1,
                "quota_probe_401": 1,
                "quota_probe_429": 0,
                "quota_probe_skipped": 0,
                "deleted_429": 0,
                "partial": True,
                "event_count": 2,
            },
        },
    )

    payload = _summary_payload(app)

    assert payload["rotate_latest_summary"]["deleted_401"] == 10
    assert payload["rotate_latest_summary"]["quota_probe_401"] == 14
    assert payload["rotate_current_summary"]["deleted_401"] == 1
    assert payload["rotate_log_tail"]["available"] is True
    assert len(payload["rotate_log_tail"]["recent_events"]) == 2
    assert payload["runtime_state_file"]["exists"] is True
    assert payload["runtime_state_file"]["path"] == str(runtime_state_file)


def test_summary_payload_exposes_account_survival_payload(monkeypatch, tmp_path: Path) -> None:
    state_file = tmp_path / "account_survival.json"
    state_file.write_text(
        (
            "{\n"
            '  "updated_at": "2026-03-26T13:10:00+08:00",\n'
            '  "summary": {"tracked": 4, "alive": 3, "invalid": 1}\n'
            "}\n"
        ),
        encoding="utf-8",
    )
    app = create_app(enable_background_tasks=False)
    app.state.settings = AppSettings(
        pool_dir=tmp_path,
        account_survival_enabled=True,
        account_survival_state_file=state_file,
        responses_survival_state_file=tmp_path / "responses_survival_missing.json",
        cleanup_enabled=False,
        validate_enabled=False,
    )
    app.state.background_tasks = []

    payload = _summary_payload(app)

    assert payload["account_survival"]["available"] is True
    assert payload["account_survival"]["summary"]["tracked"] == 4
    assert payload["routes"]["account_survival"] == "/api/account-survival"


def test_summary_payload_prefers_responses_survival_payload_when_available(tmp_path: Path) -> None:
    account_state_file = tmp_path / "account_survival.json"
    account_state_file.write_text(
        (
            "{\n"
            '  "probe_mode": "usage",\n'
            '  "summary": {"tracked": 4, "alive": 4, "invalid": 0}\n'
            "}\n"
        ),
        encoding="utf-8",
    )
    responses_state_file = tmp_path / "responses_survival.json"
    responses_state_file.write_text(
        (
            "{\n"
            '  "probe_mode": "responses",\n'
            '  "updated_at": "2026-03-30T20:30:00+08:00",\n'
            '  "summary": {"tracked": 8, "alive": 7, "invalid": 1, "first_invalid_count": 1}\n'
            "}\n"
        ),
        encoding="utf-8",
    )

    app = create_app(enable_background_tasks=False)
    app.state.settings = AppSettings(
        pool_dir=tmp_path,
        account_survival_enabled=True,
        account_survival_state_file=account_state_file,
        responses_survival_state_file=responses_state_file,
        cleanup_enabled=False,
        validate_enabled=False,
    )
    app.state.background_tasks = []

    payload = _summary_payload(app)

    assert payload["account_survival"]["available"] is True
    assert payload["account_survival"]["probe_mode"] == "responses"
    assert payload["account_survival"]["summary"]["tracked"] == 8


def test_account_survival_reseed_api_rebuilds_latest_ten_cohort(monkeypatch, tmp_path: Path) -> None:
    for idx in range(12):
        (tmp_path / f"user{idx:02d}@example.com.json").write_text(
            (
                "{\n"
                f'  "email": "user{idx:02d}@example.com",\n'
                '  "access_token": "tok",\n'
                '  "account_id": "acct",\n'
                f'  "created_at": "2026-03-26T12:{idx:02d}:00+08:00"\n'
                "}\n"
            ),
            encoding="utf-8",
        )

    state_file = tmp_path / "account_survival.json"
    state_file.write_text(
        (
            "{\n"
            '  "seed_source": "recent_existing_pool_files",\n'
            '  "members": []\n'
            "}\n"
        ),
        encoding="utf-8",
    )

    app = create_app(enable_background_tasks=False)
    app.state.settings = AppSettings(
        pool_dir=tmp_path,
        account_survival_enabled=True,
        account_survival_cohort_size=10,
        account_survival_state_file=state_file,
        responses_survival_state_file=tmp_path / "responses_survival_missing.json",
        cleanup_enabled=False,
        validate_enabled=False,
    )
    app.state.background_tasks = []

    monkeypatch.setattr(
        "ops.account_survival.classify_token_file",
        lambda *_args, **_kwargs: ScanResult(
            email="stub@example.com",
            category="normal",
            status_code=200,
            detail="ok",
        ),
    )

    status, _headers, body = _request_via_asgi(app, "POST", "/api/account-survival/reseed")
    payload = json.loads(body.decode("utf-8"))

    assert status == 200
    assert payload["available"] is True
    assert payload["summary"]["tracked"] == 10
    assert payload["seed_source"] == "latest_generated_pool_files"
    assert payload["members"][0]["email"] == "user11@example.com"


def test_summary_payload_exposes_register_burst_plan(tmp_path: Path) -> None:
    app = create_app(enable_background_tasks=False)
    app.state.settings = AppSettings(
        pool_dir=tmp_path,
        register_batch_threads=1,
        register_batch_target_count=20,
        register_batch_interval_seconds=10800,
        cleanup_enabled=False,
        validate_enabled=False,
    )
    app.state.background_tasks = []

    payload = _summary_payload(app)

    burst_plan = payload["register_burst_plan"]
    assert burst_plan["mode"] == "burst"
    assert burst_plan["threads"] == 1
    assert burst_plan["target_count"] == 20
    assert burst_plan["interval_seconds"] == 10800
    assert burst_plan["accounts_per_day"] == 160
    assert burst_plan["accounts_needed_for_one_day_target"] == 20
    assert burst_plan["accounts_needed_for_sustained_daily_target"] == 140


def test_rotate_log_tail_builds_current_summary_for_in_progress_rotate(monkeypatch, tmp_path: Path) -> None:
    log_path = tmp_path / "dashboard.log"
    log_path.write_text(
        "\n".join(
            [
                "[10:00:00] [rotate] summary | 主池: 800 → 789 | 401删除: 10 | quota探测: 20 | probe401: 10 | probe429: 1 | probe跳过: 2 | 429删除: 1",
                "[10:05:00] [rotate] 🔎 a@example.com.json quota probe → 401 invalidated",
                "[10:05:01] [rotate] ❌ a@example.com.json 401删除",
                "[10:05:02] [rotate] 🔎 b@example.com.json quota probe → 429",
                "[10:05:03] [rotate] ❌ b@example.com.json 429删除",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("main.DEFAULT_DASHBOARD_LOG_FILE", log_path)

    payload = _rotate_log_tail()

    assert payload["latest_summary"]["deleted_401"] == 10
    assert payload["current_summary"]["quota_probed"] == 2
    assert payload["current_summary"]["quota_probe_401"] == 1
    assert payload["current_summary"]["quota_probe_429"] == 1
    assert payload["current_summary"]["deleted_401"] == 1
    assert payload["current_summary"]["deleted_429"] == 1


def test_recent_pool_files_returns_latest_entries_without_glob_expansion_issue(tmp_path: Path) -> None:
    older = tmp_path / "older@example.com.json"
    newer = tmp_path / "newer@example.com.json"
    older.write_text('{"email":"older@example.com"}', encoding="utf-8")
    newer.write_text('{"email":"newer@example.com"}', encoding="utf-8")
    older.touch()
    newer.touch()

    items = _recent_pool_files(tmp_path, limit=2)

    assert len(items) == 2
    assert {item["name"] for item in items} == {"older@example.com.json", "newer@example.com.json"}
    assert all(item["size_bytes"] > 0 for item in items)


def test_summary_payload_exposes_dashboard_overview_fields(monkeypatch, tmp_path: Path) -> None:
    app = create_app(enable_background_tasks=False)
    app.state.settings = AppSettings(
        pool_dir=tmp_path,
        cleanup_enabled=False,
        validate_enabled=False,
    )
    app.state.background_tasks = []

    class FakeRegistrationLoop:
        def snapshot(self) -> dict[str, object]:
            return {
                "name": "register",
                "status": "running",
                "threads_alive": 2,
                "threads_total": 4,
                "total_attempts": 10,
                "total_success": 8,
                "total_success_registered": 8,
                "total_cpa_sync_success": 5,
                "total_cpa_sync_failure": 2,
                "total_failure": 2,
                "success_rate": 80.0,
                "registered_success_rate": 80.0,
                "cpa_sync_success_rate": 50.0,
                "target_count": None,
                "target_reached": False,
                "last_error": None,
                "proxy": None,
                "proxy_pool_enabled": False,
                "mail_provider": "mailtm",
                "interval_seconds": 5,
                "run_count": 10,
                "success_count": 8,
                "failure_count": 2,
                "is_running": True,
                "last_started_at": None,
                "last_finished_at": None,
                "last_duration_seconds": None,
                "next_run_at": None,
                "failure_by_stage": {},
                "failure_signals": {},
                "recent_failure_hotspots": [],
                "recent_attempts": [],
                "cfmail_add_phone_stoploss": {
                    "active_domain": "demo.example.test",
                    "in_cooldown": True,
                    "cooldown_remaining_seconds": 120,
                    "last_triggered_at": "2026-03-23T00:00:00",
                    "last_reason": "add_phone threshold reached",
                    "last_add_phone_failures": 8,
                    "last_successes": 0,
                    "last_window_size": 12,
                    "window_size": 12,
                    "threshold": 8,
                    "max_successes_in_window": 2,
                },
            }

    app.state.registration_loop = FakeRegistrationLoop()
    monkeypatch.setattr(
        "main._fetch_management_auth_files",
        lambda settings: (
            True,
            [
                {"name": "a@example.com.json", "unavailable": False},
                {"name": "b@example.com.json", "unavailable": True, "status_message": "usage_limit_reached"},
                {"name": "c@example.com.json", "status_message": "token invalidated by upstream"},
            ],
        ),
    )
    monkeypatch.setattr("main._count_today_new", lambda pool_dir: 3)

    payload = _summary_payload(app)

    assert payload["cpa_count"] == 3
    assert payload["regular_accounts"]["source_available"] is True
    assert payload["regular_accounts"]["available"] == 1
    assert payload["regular_accounts"]["waiting_reset"] == 1
    assert payload["regular_accounts"]["invalid"] == 1
    assert payload["tokens"]["estimation_mode"] == "count_based"
    assert payload["tokens"]["baseline_source"] == "configured"
    assert payload["tokens"]["available_now"] == 5000000
    assert payload["tokens"]["available_with_reset"] == 10000000
    assert payload["today_new"] == 3
    assert payload["success_rate"] == 80.0
    assert payload["registered_success_total"] == 8
    assert payload["cpa_sync_success_total"] == 5
    assert payload["cpa_sync_failure_total"] == 2
    assert payload["registered_success_rate"] == 80.0
    assert payload["cpa_sync_success_rate"] == 50.0
    assert payload["observed_loss"] == 2
    assert payload["register_failure_by_stage"] == {}
    assert payload["register_failure_signals"] == {}
    assert payload["register_recent_failure_hotspots"] == []
    assert payload["register_cfmail_add_phone_stoploss"]["in_cooldown"] is True


def test_summary_payload_falls_back_when_management_inventory_unavailable(monkeypatch, tmp_path: Path) -> None:
    app = create_app(enable_background_tasks=False)
    app.state.settings = AppSettings(
        pool_dir=tmp_path,
        runtime_state_file=tmp_path / "runtime_state.json",
        cleanup_enabled=False,
        validate_enabled=False,
    )
    app.state.background_tasks = []
    app.state.registration_loop = None

    monkeypatch.setattr("main._fetch_management_auth_files", lambda settings: (False, []))
    monkeypatch.setattr("main._count_cpa_files", lambda settings: 9)

    payload = _summary_payload(app)

    assert payload["cpa_count"] == 9
    assert payload["cpa_inventory"]["management_available"] is False
    assert payload["regular_accounts"]["source_available"] is False
    assert payload["regular_accounts"]["source_error"] == "management_data_unavailable"
    assert payload["tokens"]["estimation_mode"] == "count_based"
    assert payload["tokens"]["fallback_reason"] == "missing_management_inventory"
    assert payload["success_rate"] is None


def test_runtime_payload_exposes_proxy_pool_snapshot(tmp_path: Path) -> None:
    app = create_app(enable_background_tasks=False)
    app.state.settings = AppSettings(
        pool_dir=tmp_path,
        cleanup_enabled=False,
        validate_enabled=False,
    )
    app.state.background_tasks = []

    class FakePool:
        def snapshot(self) -> list[dict[str, object]]:
            return [
                {
                    "name": "sg-node-1",
                    "region": "sg",
                    "proxy_url": "socks5://127.0.0.1:17891",
                    "local_port": 17891,
                    "in_use": True,
                    "disabled": False,
                    "successes": 7,
                    "failures": 1,
                    "last_error": "",
                }
            ]

    class FakeRegistrationLoop:
        def __init__(self) -> None:
            self._proxy_pool = FakePool()

        def snapshot(self) -> dict[str, object]:
            return {
                "name": "register",
                "status": "running",
                "threads_alive": 1,
                "threads_total": 1,
                "total_attempts": 8,
                "total_success": 7,
                "total_failure": 1,
                "success_rate": 87.5,
                "target_count": None,
                "target_reached": False,
                "last_error": None,
                "proxy": None,
                "proxy_pool_enabled": True,
                "mail_provider": "mailtm",
                "interval_seconds": 5,
                "run_count": 8,
                "success_count": 7,
                "failure_count": 1,
                "is_running": True,
                "last_started_at": None,
                "last_finished_at": None,
                "last_duration_seconds": None,
                "next_run_at": None,
            }

    app.state.registration_loop = FakeRegistrationLoop()

    payload = _runtime_payload(app)
    proxy_pool = payload["proxy_pool"]

    assert proxy_pool["enabled"] is True
    assert proxy_pool["node_count"] == 1
    assert proxy_pool["in_use_count"] == 1
    assert proxy_pool["disabled_count"] == 0
    assert proxy_pool["nodes"][0]["name"] == "sg-node-1"


def test_runtime_payload_uses_external_runtime_state_when_loop_runs_out_of_process(tmp_path: Path) -> None:
    runtime_state_file = tmp_path / "runtime_state.json"
    runtime_state_file.write_text(
        """
{
  "updated_at": "2026-03-22T22:40:00",
  "register_snapshot": {
    "name": "register",
    "status": "running",
    "threads_alive": 3,
    "threads_total": 3,
    "total_attempts": 12,
    "total_success": 9,
    "total_failure": 3,
    "success_rate": 75.0,
    "target_count": null,
    "target_reached": false,
    "last_error": "token acquisition failed",
    "proxy": "http://127.0.0.1:7899",
    "proxy_pool_enabled": true,
    "mail_provider": "cfmail",
    "interval_seconds": 5,
    "run_count": 12,
    "success_count": 9,
    "failure_count": 3,
    "is_running": true,
    "last_started_at": "2026-03-22T22:39:00",
    "last_finished_at": null,
    "last_duration_seconds": null,
    "next_run_at": null,
    "failure_by_stage": {"token_acquisition": 3},
    "failure_signals": {"add_phone_gate": 2},
    "recent_failure_hotspots": [{"key": "add_phone_gate", "stage": "add_phone_gate", "count": 2}],
    "recent_attempts": [{"timestamp": "2026-03-22T22:39:30", "success": false, "stage": "add_phone_gate", "signal": "add_phone_gate"}],
    "cfmail_add_phone_stoploss": {"active_domain": "demo.example.test", "in_cooldown": true}
  },
  "proxy_pool": {
    "configured": true,
    "enabled": true,
    "snapshot_error": null,
    "node_count": 2,
    "in_use_count": 1,
    "disabled_count": 0,
    "nodes": [
      {"name": "sg-1", "in_use": true, "disabled": false},
      {"name": "tw-1", "in_use": false, "disabled": false}
    ]
  }
}
""".strip(),
        encoding="utf-8",
    )

    app = create_app(enable_background_tasks=False)
    app.state.settings = AppSettings(
        pool_dir=tmp_path,
        runtime_state_file=runtime_state_file,
        cleanup_enabled=False,
        validate_enabled=False,
    )
    app.state.background_tasks = []
    app.state.registration_loop = None

    payload = _runtime_payload(app)

    assert payload["architecture"] == "split-runtime-fastapi+loop"
    register_state = next(task for task in payload["task_states"] if task["name"] == "register")
    assert register_state["threads_alive"] == 3
    assert register_state["failure_by_stage"]["token_acquisition"] == 3
    assert register_state["cfmail_add_phone_stoploss"]["in_cooldown"] is True
    assert payload["proxy_pool"]["enabled"] is True
    assert payload["proxy_pool"]["node_count"] == 2


def test_runtime_payload_marks_proxy_pool_configured_for_direct_urls(tmp_path: Path) -> None:
    app = create_app(enable_background_tasks=False)
    app.state.settings = AppSettings(
        pool_dir=tmp_path,
        runtime_state_file=tmp_path / "runtime_state.json",
        proxy_pool_direct_urls="http://5.6.7.8:8080",
        cleanup_enabled=False,
        validate_enabled=False,
    )
    app.state.background_tasks = []
    app.state.registration_loop = None

    payload = _runtime_payload(app)

    assert payload["proxy_pool"]["configured"] is True
    assert payload["proxy_pool"]["enabled"] is False


def test_dashboard_cors_preflight_allows_configured_origin(monkeypatch) -> None:
    monkeypatch.setenv("ZHUCE6_DASHBOARD_ALLOWED_ORIGINS", "http://127.0.0.1:8317")
    app = create_app(enable_background_tasks=False)
    app.state.settings = AppSettings(cleanup_enabled=False, validate_enabled=False)
    app.state.background_tasks = []
    app.state.registration_loop = None

    status_code, headers, _ = _request_via_asgi(
        app,
        "OPTIONS",
        "/api/summary",
        {
            "Origin": "http://127.0.0.1:8317",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert status_code == 204
    assert headers["access-control-allow-origin"] == "http://127.0.0.1:8317"


def test_settings_api_returns_current_runtime_mode(tmp_path: Path) -> None:
    app = create_app(enable_background_tasks=False, mode="lite")
    app.state.settings = AppSettings(
        runtime_mode="lite",
        register_enabled=True,
        register_threads=2,
        register_batch_target_count=30,
        register_batch_interval_seconds=3600,
        register_mail_provider="cfmail",
        register_proxy="http://127.0.0.1:7899",
        enable_proxy_pool=True,
        proxy_pool_size=10,
        proxy_pool_direct_urls="http://1.2.3.4:8080",
        proxy_pool_regions=("jp", "tw"),
                rotate_enabled=False,
        rotate_interval=120,
        pool_dir=tmp_path,
    )
    app.state.background_tasks = []
    app.state.registration_loop = None

    status, _headers, body = _request_via_asgi(app, "GET", "/api/settings")
    payload = json.loads(body.decode("utf-8"))

    assert status == 200
    assert payload["mode"] == "lite"
    assert payload["register"]["threads"] == 2
    assert payload["proxy_pool"]["size"] == 10
    assert payload["cpa"]["rotate_interval"] == 120


def test_settings_api_persists_whitelisted_updates(monkeypatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("ZHUCE6_ENV_FILE", str(env_file))
    app = create_app(enable_background_tasks=False, mode="full")
    app.state.settings = AppSettings(
        runtime_mode="full",
        register_threads=1,
        register_batch_target_count=20,
        register_batch_interval_seconds=10800,
        register_mail_provider="cfmail",
        register_proxy="http://127.0.0.1:7899",
        enable_proxy_pool=True,
        proxy_pool_size=20,
        proxy_pool_direct_urls="",
        proxy_pool_regions=("jp", "tw", "hk", "sg"),
                rotate_interval=120,
        pool_dir=tmp_path,
    )
    app.state.background_tasks = []
    app.state.registration_loop = None

    status, _headers, body = _request_via_asgi(
        app,
        "PUT",
        "/api/settings",
        headers={"content-type": "application/json"},
        body=json.dumps(
            {
                "register.threads": 3,
                "register.batch_target_count": 25,
                "proxy_pool.size": 12,
                "cpa.rotate_interval": 300,
            }
        ).encode("utf-8"),
    )
    payload = json.loads(body.decode("utf-8"))
    persisted = env_file.read_text(encoding="utf-8")

    assert status == 200
    assert payload["register"]["threads"] == 3
    assert payload["proxy_pool"]["size"] == 12
    assert payload["cpa"]["rotate_interval"] == 300
    assert payload["restart_required"] is True
    assert "ZHUCE6_REGISTER_THREADS=3" in persisted
    assert "ZHUCE6_REGISTER_BATCH_TARGET_COUNT=25" in persisted
    assert "ZHUCE6_PROXY_POOL_SIZE=12" in persisted
    assert "ZHUCE6_ROTATE_INTERVAL=300" in persisted


def test_register_control_api_starts_and_stops_loop(monkeypatch, tmp_path: Path) -> None:
    events: list[str] = []

    class FakeLoop:
        def __init__(self, settings):  # type: ignore[no-untyped-def]
            self.settings = settings

        def start(self) -> None:
            events.append("start")

        def stop(self) -> None:
            events.append("stop")

    monkeypatch.setattr("main.RegistrationLoop", FakeLoop)

    app = create_app(enable_background_tasks=False, mode="dashboard")
    app.state.settings = AppSettings(runtime_mode="dashboard", pool_dir=tmp_path)
    app.state.background_tasks = []
    app.state.registration_loop = None

    status, _headers, body = _request_via_asgi(
        app,
        "POST",
        "/api/control/register",
        headers={"content-type": "application/json"},
        body=b'{"action":"start"}',
    )
    payload = json.loads(body.decode("utf-8"))
    assert status == 200
    assert payload["status"] == "started"
    assert events == ["start"]

    status, _headers, body = _request_via_asgi(
        app,
        "POST",
        "/api/control/register",
        headers={"content-type": "application/json"},
        body=b'{"action":"stop"}',
    )
    payload = json.loads(body.decode("utf-8"))
    assert status == 200
    assert payload["status"] == "stopped"
    assert events == ["start", "stop"]


def test_health_dependencies_api_skips_cpa_checks_in_lite_mode(monkeypatch, tmp_path: Path) -> None:
    def fail_fetch(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("lite mode should not query CPA")

    monkeypatch.setattr("main._fetch_management_auth_files", fail_fetch)

    app = create_app(enable_background_tasks=False, mode="lite")
    app.state.settings = AppSettings(
        runtime_mode="lite",
        register_mail_provider="cfmail",
        enable_proxy_pool=False,
        pool_dir=tmp_path,
    )
    app.state.background_tasks = []
    app.state.registration_loop = None

    status, _headers, body = _request_via_asgi(app, "GET", "/api/health/dependencies")
    payload = json.loads(body.decode("utf-8"))

    assert status == 200
    assert payload["cpa"]["status"] == "unconfigured"
    assert "docker" not in payload


def test_lite_mode_summary_skips_management_inventory(monkeypatch, tmp_path: Path) -> None:
    def fail_fetch(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("lite mode should not fetch CPA inventory")

    monkeypatch.setattr("main._fetch_management_auth_files", fail_fetch)

    app = create_app(enable_background_tasks=False, mode="lite")
    app.state.settings = AppSettings(runtime_mode="lite", pool_dir=tmp_path)
    app.state.background_tasks = []
    app.state.registration_loop = None

    payload = _summary_payload(app)

    assert payload["runtime"]["runtime_mode"] == "lite"
    assert payload["cpa_count"] is None
    assert payload["regular_accounts"] is None
    assert payload["tokens"] is None


def test_dashboard_html_contains_settings_tab_and_control_api_hooks() -> None:
    html = Path("/home/sophomores/zhuce6/dashboard/zhuce6.html").read_text(encoding="utf-8")

    assert "Settings" in html
    assert "/api/settings" in html
    assert "/api/control/register" in html
    assert "/api/health/dependencies" in html
    assert "http://localhost:8317/management.html" not in html
    assert "settings.cpa.management_url" in html


def test_create_app_lite_mode_registers_only_register_task(monkeypatch) -> None:
    class FakeLoop:
        def __init__(self, settings):  # type: ignore[no-untyped-def]
            self.settings = settings

        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

        def snapshot(self) -> dict[str, object]:
            return {
                "name": "register",
                "status": "running",
                "run_count": 0,
                "success_count": 0,
                "failure_count": 0,
                "threads_alive": 1,
                "threads_total": 1,
            }

    monkeypatch.setattr("main.RegistrationLoop", FakeLoop)

    async def run_lifespan() -> None:
        app = create_app(enable_background_tasks=True, mode="lite")
        async with app.router.lifespan_context(app):
            payload = _runtime_payload(app)
            assert payload["runtime_mode"] == "lite"
            assert payload["registered_tasks"] == ["register"]

    asyncio.run(run_lifespan())


def test_dashboard_cors_get_adds_origin_header_for_allowed_origin(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ZHUCE6_DASHBOARD_ALLOWED_ORIGINS", "http://localhost:8317")
    app = create_app(enable_background_tasks=False)
    app.state.settings = AppSettings(pool_dir=tmp_path, cleanup_enabled=False, validate_enabled=False)
    app.state.background_tasks = []
    app.state.registration_loop = None

    status_code, headers, _ = _request_via_asgi(
        app,
        "GET",
        "/api/summary",
        {"Origin": "http://localhost:8317"},
    )

    assert status_code == 200
    assert headers["access-control-allow-origin"] == "http://localhost:8317"


def test_dashboard_cors_headers_are_not_added_for_other_origins(monkeypatch) -> None:
    monkeypatch.setenv("ZHUCE6_DASHBOARD_ALLOWED_ORIGINS", "http://localhost:8317")
    app = create_app(enable_background_tasks=False)
    app.state.settings = AppSettings(cleanup_enabled=False, validate_enabled=False)
    app.state.background_tasks = []
    app.state.registration_loop = None

    status_code, headers, _ = _request_via_asgi(
        app,
        "GET",
        "/api/runtime",
        {"Origin": "http://127.0.0.1:9999"},
    )

    assert status_code == 200
    assert "access-control-allow-origin" not in headers


def test_build_background_tasks_registers_validate_when_enabled() -> None:
    tasks = _build_background_tasks(
        AppSettings(
                cleanup_enabled=False,
            d1_cleanup_enabled=False,
            validate_enabled=True,
            validate_interval=90,
            validate_scope="used",
            rotate_enabled=False,
            account_survival_enabled=False,
        )
    )

    assert [task.name for task in tasks] == ["validate"]
    assert tasks[0].interval_seconds == 90


def test_build_background_tasks_registers_d1_cleanup_when_enabled() -> None:
    tasks = _build_background_tasks(
        AppSettings(
                cleanup_enabled=False,
            validate_enabled=False,
            rotate_enabled=False,
            d1_cleanup_enabled=True,
            d1_cleanup_interval=1800,
            account_survival_enabled=False,
        )
    )

    assert [task.name for task in tasks] == ["d1_cleanup"]
    assert tasks[0].interval_seconds == 1800

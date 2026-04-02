import json
from dataclasses import replace
from pathlib import Path

from core.base_mailbox import MailboxAccount
from core.settings import AppSettings
from core.cfmail_domain_rotation import DomainHealthTracker
from core.cfmail_provisioner import ProvisionResult
from main import RegistrationBurstScheduler, RegistrationLoop


def _base_settings(**overrides) -> AppSettings:
    settings = AppSettings(
        cleanup_enabled=False,
        register_sleep_min=0,
        register_sleep_max=0,
        register_mail_provider="mailtm,mailgw",
    )
    return replace(settings, **overrides)


def test_registration_loop_switches_provider_after_configured_failures(monkeypatch) -> None:
    settings = _base_settings(register_max_consecutive_failures=2)
    loop = RegistrationLoop(settings)
    loop._providers = ["mailtm", "mailgw"]
    calls: list[str] = []

    def fake_run_chatgpt_register_once(**kwargs):  # type: ignore[no-untyped-def]
        calls.append(kwargs["mail_provider"])
        if len(calls) >= 3:
            loop._stop_event.set()
        return {"success": False, "error_message": "failed"}

    monkeypatch.setattr("main.run_chatgpt_register_once", fake_run_chatgpt_register_once)

    loop._worker(thread_id=1, initial_provider="mailtm")

    assert calls == ["mailtm", "mailtm", "mailgw"]


def test_registration_loop_stops_when_target_count_reached(monkeypatch) -> None:
    settings = _base_settings(register_target_count=2, register_mail_provider="mailtm")
    loop = RegistrationLoop(settings)
    loop._providers = ["mailtm"]
    calls: list[str] = []

    def fake_run_chatgpt_register_once(**kwargs):  # type: ignore[no-untyped-def]
        calls.append(kwargs["mail_provider"])
        return {"success": True, "email": f"user{len(calls)}@example.com"}

    monkeypatch.setattr("main.run_chatgpt_register_once", fake_run_chatgpt_register_once)
    monkeypatch.setattr(loop, "_sync_cpa_from_success", lambda result, thread_id: (True, "", str(result.get("email") or "")))

    loop._worker(thread_id=1, initial_provider="mailtm")

    assert calls == ["mailtm", "mailtm"]
    assert loop._target_reached.is_set() is True


def test_registration_loop_uses_register_proxy_when_proxy_pool_disabled(monkeypatch) -> None:
    settings = _base_settings(register_proxy="http://127.0.0.1:7890", register_mail_provider="mailtm")
    loop = RegistrationLoop(settings)
    loop._providers = ["mailtm"]
    proxies: list[str | None] = []

    def fake_run_chatgpt_register_once(**kwargs):  # type: ignore[no-untyped-def]
        proxies.append(kwargs["proxy"])
        loop._stop_event.set()
        return {"success": False, "error_message": "failed"}

    monkeypatch.setattr("main.run_chatgpt_register_once", fake_run_chatgpt_register_once)

    loop._worker(thread_id=1, initial_provider="mailtm")

    assert proxies == ["http://127.0.0.1:7890"]


def test_registration_loop_caps_pending_token_retry_delay_to_one_minute(monkeypatch) -> None:
    monkeypatch.setenv("ZHUCE6_PENDING_TOKEN_RETRY_DELAY_SECONDS", "300")

    loop = RegistrationLoop(_base_settings())

    assert loop._pending_token_retry_delay_seconds == 60


def test_registration_loop_uses_proxy_pool_and_releases_lease(monkeypatch) -> None:
    settings = _base_settings(register_mail_provider="mailtm")
    loop = RegistrationLoop(settings)
    loop._providers = ["mailtm"]
    recorded: dict[str, object] = {}

    class FakeLease:
        name = "sg-node"
        local_port = 17891
        proxy_url = "socks5://127.0.0.1:17891"

    class FakePool:
        def acquire(self, timeout=5.0):  # type: ignore[no-untyped-def]
            recorded["timeout"] = timeout
            return FakeLease()

        def release(self, lease, *, success, stage=None):  # type: ignore[no-untyped-def]
            recorded["released"] = (lease.proxy_url, success, stage)

    def fake_run_chatgpt_register_once(**kwargs):  # type: ignore[no-untyped-def]
        recorded["proxy"] = kwargs["proxy"]
        loop._stop_event.set()
        return {"success": True, "email": "demo@example.com"}

    loop._proxy_pool = FakePool()
    monkeypatch.setattr("main.run_chatgpt_register_once", fake_run_chatgpt_register_once)
    monkeypatch.setattr(loop, "_sync_cpa_from_success", lambda result, thread_id: (True, "", str(result.get("email") or "")))

    loop._worker(thread_id=1, initial_provider="mailtm")

    assert recorded["proxy"] == "socks5://127.0.0.1:17891"
    assert recorded["released"] == ("socks5://127.0.0.1:17891", True, "completed")


def test_registration_loop_starts_proxy_pool_for_direct_urls(monkeypatch) -> None:
    settings = _base_settings(
        register_mail_provider="mailtm",
        register_threads=1,
        proxy_pool_direct_urls="http://5.6.7.8:8080",
    )
    loop = RegistrationLoop(settings)
    recorded: dict[str, object] = {}

    class FakePool:
        def start(self) -> None:
            recorded["pool_started"] = True

    class FakeThread:
        def __init__(self, target=None, args=(), daemon=None, name=None):  # type: ignore[no-untyped-def]
            recorded["thread_args"] = {"target": target, "args": args, "daemon": daemon, "name": name}

        def start(self) -> None:
            recorded["thread_started"] = True

    monkeypatch.setattr("main.load_all", lambda: None)
    monkeypatch.setattr("core.proxy_pool.ProxyPool.from_settings", lambda settings: FakePool())
    monkeypatch.setattr("main.threading.Thread", FakeThread)
    monkeypatch.setattr("core.registration._maybe_reconcile_cpa_runtime", lambda **kwargs: None)
    monkeypatch.setattr(loop, "_write_runtime_state", lambda: None)

    loop.start()

    assert recorded["pool_started"] is True
    assert recorded["thread_started"] is True
    assert loop._proxy_pool is not None


def test_registration_loop_start_reconciles_pool_backups_to_cpa(monkeypatch, tmp_path: Path) -> None:
    settings = _base_settings(
        register_mail_provider="mailtm",
        register_threads=1,
        backend="cpa",
        cpa_runtime_reconcile_enabled=True,
        pool_dir=tmp_path / "pool",
    )
    loop = RegistrationLoop(settings)
    recorded: dict[str, object] = {}

    class FakeThread:
        def __init__(self, target=None, args=(), daemon=None, name=None):  # type: ignore[no-untyped-def]
            recorded["thread_args"] = {"target": target, "args": args, "daemon": daemon, "name": name}

        def start(self) -> None:
            recorded["thread_started"] = True

    monkeypatch.setattr("main.load_all", lambda: None)
    monkeypatch.setattr("main.threading.Thread", FakeThread)
    monkeypatch.setattr("core.registration.create_backend_client", lambda settings: "backend-client")
    monkeypatch.setattr(
        "core.registration._maybe_reconcile_cpa_runtime",
        lambda **kwargs: recorded.setdefault("reconcile", kwargs),
    )
    monkeypatch.setattr(loop, "_write_runtime_state", lambda: None)

    loop.start()

    assert recorded["thread_started"] is True
    assert recorded["reconcile"]["pool_dir"] == settings.pool_dir
    assert recorded["reconcile"]["client"] == "backend-client"


def test_registration_loop_start_normalizes_cfmail_to_single_active_domain(monkeypatch) -> None:
    settings = _base_settings(
        register_mail_provider="cfmail",
        register_threads=1,
        backend="cpa",
        cpa_runtime_reconcile_enabled=False,
    )
    loop = RegistrationLoop(settings)
    recorded: dict[str, object] = {}

    class FakeThread:
        def __init__(self, target=None, args=(), daemon=None, name=None):  # type: ignore[no-untyped-def]
            recorded["thread_args"] = {"target": target, "args": args, "daemon": daemon, "name": name}

        def start(self) -> None:
            recorded["thread_started"] = True

    class FakeProvisioner:
        def __init__(self, *, proxy_url=None, **_kwargs):  # type: ignore[no-untyped-def]
            recorded["proxy_url"] = proxy_url

        def normalize_accounts_to_single_active_domain(self):  # type: ignore[no-untyped-def]
            recorded["normalized"] = True
            return {
                "active_domain": "auto-live.example.test",
                "removed_domains": ["auto-old.example.test"],
            }

    class FakeManager:
        def reload_if_needed(self, force=False):  # type: ignore[no-untyped-def]
            recorded["reload_force"] = force
            return True

    monkeypatch.setattr("main.load_all", lambda: None)
    monkeypatch.setattr("main.threading.Thread", FakeThread)
    monkeypatch.setattr("core.cfmail_provisioner.CfmailProvisioner", FakeProvisioner)
    monkeypatch.setattr("core.cfmail.DEFAULT_CFMAIL_MANAGER", FakeManager())
    monkeypatch.setattr(loop, "_ensure_cfmail_active_domain_ready", lambda: True)
    monkeypatch.setattr(loop, "_write_runtime_state", lambda: None)
    monkeypatch.setattr(loop, "_log", lambda message: recorded.setdefault("logs", []).append(message))

    loop.start()

    assert recorded["thread_started"] is True
    assert recorded["proxy_url"] == settings.register_proxy
    assert recorded["normalized"] is True
    assert recorded["reload_force"] is True
    assert any("normalized active domain state" in line for line in recorded["logs"])


def test_registration_loop_retries_device_id_once_with_new_proxy(monkeypatch) -> None:
    settings = _base_settings(register_mail_provider="mailtm")
    loop = RegistrationLoop(settings)
    loop._providers = ["mailtm"]
    recorded: dict[str, object] = {"releases": []}

    class FakeLease:
        def __init__(self, name: str, local_port: int) -> None:
            self.name = name
            self.local_port = local_port
            self.proxy_url = f"socks5://127.0.0.1:{local_port}"

    class FakePool:
        def __init__(self) -> None:
            self._leases = [FakeLease("tw-bad", 17891), FakeLease("sg-good", 17892)]

        def acquire(self, timeout=5.0):  # type: ignore[no-untyped-def]
            recorded.setdefault("timeouts", []).append(timeout)
            return self._leases.pop(0)

        def release(self, lease, *, success, stage=None):  # type: ignore[no-untyped-def]
            recorded["releases"].append((lease.name, success, stage))

    attempts: list[str] = []

    def fake_run_chatgpt_register_once(**kwargs):  # type: ignore[no-untyped-def]
        attempts.append(kwargs["proxy"])
        if len(attempts) == 1:
            return {
                "success": False,
                "stage": "device_id",
                "error_message": "device id acquisition failed",
            }
        loop._stop_event.set()
        return {"success": True, "stage": "completed", "email": "demo@example.com"}

    loop._proxy_pool = FakePool()
    monkeypatch.setattr("main.run_chatgpt_register_once", fake_run_chatgpt_register_once)
    monkeypatch.setattr(loop, "_sync_cpa_from_success", lambda result, thread_id: (True, "", str(result.get("email") or "")))

    loop._worker(thread_id=1, initial_provider="mailtm")

    assert attempts == ["socks5://127.0.0.1:17891", "socks5://127.0.0.1:17892"]
    assert recorded["releases"] == [
        ("tw-bad", False, "device_id"),
        ("sg-good", True, "completed"),
    ]
    snapshot = loop.snapshot()
    assert snapshot["total_attempts"] == 1
    assert snapshot["total_success"] == 1
    assert snapshot["total_failure"] == 0


def test_registration_loop_syncs_cpa_immediately_after_success(monkeypatch, tmp_path: Path) -> None:
    settings = _base_settings(
        register_mail_provider="mailtm",
    )
    loop = RegistrationLoop(settings)
    loop._providers = ["mailtm"]
    pool_file = tmp_path / "fresh@example.com.json"
    pool_file.write_text(json.dumps({"email": "fresh@example.com", "access_token": "tok", "account_id": "acct"}), encoding="utf-8")
    recorded: dict[str, object] = {}

    def fake_run_chatgpt_register_once(**kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        loop._stop_event.set()
        return {
            "success": True,
            "email": "fresh@example.com",
            "pool_file": str(pool_file),
            "written_to_pool": True,
        }

    monkeypatch.setattr("main.run_chatgpt_register_once", fake_run_chatgpt_register_once)
    monkeypatch.setattr("core.registration.get_management_key", lambda: "secret")  # type: ignore[no-untyped-def]
    monkeypatch.setattr("main.classify_token_file", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("readiness probe should not gate direct CPA sync")))  # type: ignore[no-untyped-def]

    def fake_upload_to_cpa(token_data, api_url=None, api_key=None, proxy=None):  # type: ignore[no-untyped-def]
        recorded["token_data"] = token_data
        recorded["api_url"] = api_url
        recorded["api_key"] = api_key
        recorded["proxy"] = proxy
        return True, "upload success"

    monkeypatch.setattr("platforms.chatgpt.cpa_upload.upload_to_cpa", fake_upload_to_cpa)

    loop._worker(thread_id=1, initial_provider="mailtm")

    assert recorded["token_data"]["email"] == "fresh@example.com"
    assert recorded["api_url"] == "http://127.0.0.1:8317"
    assert recorded["api_key"] == "secret"
    assert recorded["proxy"] is None
    payload = json.loads(pool_file.read_text(encoding="utf-8"))
    assert payload["cpa_sync_status"] == "synced"
    snapshot = loop.snapshot()
    assert snapshot["total_success"] == 1
    assert snapshot["total_success_registered"] == 1
    assert snapshot["total_cpa_sync_success"] == 1
    assert snapshot["total_cpa_sync_failure"] == 0
    assert snapshot["registered_success_rate"] == 100.0
    assert snapshot["cpa_sync_success_rate"] == 100.0


def test_registration_loop_syncs_cpa_before_breaking_on_target_reached(monkeypatch, tmp_path: Path) -> None:
    settings = _base_settings(
        register_mail_provider="mailtm",
        register_target_count=1,
    )
    loop = RegistrationLoop(settings)
    loop._providers = ["mailtm"]
    pool_file = tmp_path / "target@example.com.json"
    pool_file.write_text(json.dumps({"email": "target@example.com", "access_token": "tok", "account_id": "acct"}), encoding="utf-8")
    recorded: dict[str, object] = {}

    def fake_run_chatgpt_register_once(**kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        return {
            "success": True,
            "email": "target@example.com",
            "pool_file": str(pool_file),
            "written_to_pool": True,
        }

    monkeypatch.setattr("main.run_chatgpt_register_once", fake_run_chatgpt_register_once)
    monkeypatch.setattr("core.registration.get_management_key", lambda: "secret")  # type: ignore[no-untyped-def]
    monkeypatch.setattr("main.classify_token_file", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("readiness probe should not gate direct CPA sync")))  # type: ignore[no-untyped-def]

    def fake_upload_to_cpa(token_data, api_url=None, api_key=None, proxy=None):  # type: ignore[no-untyped-def]
        recorded["token_data"] = token_data
        recorded["api_url"] = api_url
        recorded["api_key"] = api_key
        recorded["proxy"] = proxy
        return True, "upload success"

    monkeypatch.setattr("platforms.chatgpt.cpa_upload.upload_to_cpa", fake_upload_to_cpa)

    loop._worker(thread_id=1, initial_provider="mailtm")

    assert loop._target_reached.is_set() is True
    assert recorded["token_data"]["email"] == "target@example.com"
    payload = json.loads(pool_file.read_text(encoding="utf-8"))
    assert payload["cpa_sync_status"] == "synced"
    snapshot = loop.snapshot()
    assert snapshot["total_attempts"] == 1
    assert snapshot["total_success"] == 1
    assert snapshot["total_cpa_sync_success"] == 1
    assert snapshot["total_cpa_sync_failure"] == 0


def test_registration_loop_marks_pool_backup_when_direct_cpa_sync_fails(monkeypatch, tmp_path: Path) -> None:
    settings = _base_settings(
        register_mail_provider="mailtm",
    )
    loop = RegistrationLoop(settings)
    loop._providers = ["mailtm"]
    pool_file = tmp_path / "fresh@example.com.json"
    pool_file.write_text(json.dumps({"email": "fresh@example.com", "access_token": "tok", "account_id": "acct"}), encoding="utf-8")

    def fake_run_chatgpt_register_once(**kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        loop._stop_event.set()
        return {
            "success": True,
            "email": "fresh@example.com",
            "pool_file": str(pool_file),
            "written_to_pool": True,
        }

    monkeypatch.setattr("main.run_chatgpt_register_once", fake_run_chatgpt_register_once)
    monkeypatch.setattr("core.registration.get_management_key", lambda: "secret")  # type: ignore[no-untyped-def]
    monkeypatch.setattr("main.classify_token_file", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("readiness probe should not gate direct CPA sync")))  # type: ignore[no-untyped-def]
    monkeypatch.setattr("platforms.chatgpt.cpa_upload.upload_to_cpa", lambda *args, **kwargs: (False, "unexpected EOF"))  # type: ignore[no-untyped-def]

    loop._worker(thread_id=1, initial_provider="mailtm")

    payload = json.loads(pool_file.read_text(encoding="utf-8"))
    assert payload["backup_written"] is True
    assert payload["cpa_sync_status"] == "failed"
    assert payload["last_cpa_sync_error"] == "unexpected EOF"
    snapshot = loop.snapshot()
    assert snapshot["total_success"] == 0
    assert snapshot["total_failure"] == 1
    assert snapshot["total_cpa_sync_success"] == 0
    assert snapshot["total_cpa_sync_failure"] == 1
    assert snapshot["failure_by_stage"]["cpa_sync"] == 1
    assert snapshot["failure_signals"]["cpa_sync_failed"] == 1


def test_registration_loop_syncs_add_phone_gated_success_directly_to_cpa(monkeypatch, tmp_path: Path) -> None:
    settings = _base_settings(
        register_mail_provider="cfmail",
    )
    loop = RegistrationLoop(settings)
    loop._providers = ["cfmail"]
    pool_file = tmp_path / "fresh@example.com.json"
    pool_file.write_text(json.dumps({"email": "fresh@example.com", "access_token": "tok", "account_id": "acct"}), encoding="utf-8")
    uploaded: list[str] = []

    def fake_run_chatgpt_register_once(**kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        loop._stop_event.set()
        return {
            "success": True,
            "email": "fresh@example.com",
            "pool_file": str(pool_file),
            "written_to_pool": True,
            "metadata": {
                "mail_provider": "cfmail",
                "email_domain": "demo.example.test",
                "post_create_gate": "add_phone",
            },
        }

    monkeypatch.setattr("main.run_chatgpt_register_once", fake_run_chatgpt_register_once)
    monkeypatch.setattr("core.registration.get_management_key", lambda: "secret")  # type: ignore[no-untyped-def]
    monkeypatch.setattr("main.classify_token_file", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("add_phone success should sync directly to CPA without readiness probe")))  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "platforms.chatgpt.cpa_upload.upload_to_cpa",
        lambda token_data, api_url=None, api_key=None, proxy=None: uploaded.append(token_data["email"]) or (True, "ok"),
    )

    loop._worker(thread_id=1, initial_provider="cfmail")

    assert uploaded == ["fresh@example.com"]
    snapshot = loop.snapshot()
    assert snapshot["total_success"] == 1
    assert snapshot["total_success_registered"] == 1
    assert snapshot["total_cpa_sync_success"] == 1
    assert snapshot["total_cpa_sync_failure"] == 0
    assert snapshot["registered_success_rate"] == 100.0
    assert snapshot["cpa_sync_success_rate"] == 100.0


def test_registration_loop_preserves_backup_when_add_phone_sync_fails(monkeypatch, tmp_path: Path) -> None:
    settings = _base_settings(
        register_mail_provider="cfmail",
    )
    loop = RegistrationLoop(settings)
    loop._providers = ["cfmail"]
    pool_file = tmp_path / "fresh@example.com.json"
    pool_file.write_text(
        json.dumps({"email": "fresh@example.com", "access_token": "tok", "account_id": "acct"}),
        encoding="utf-8",
    )

    def fake_run_chatgpt_register_once(**kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        loop._stop_event.set()
        return {
            "success": True,
            "email": "fresh@example.com",
            "pool_file": str(pool_file),
            "written_to_pool": True,
            "metadata": {
                "mail_provider": "cfmail",
                "email_domain": "demo.example.test",
                "post_create_gate": "add_phone",
            },
        }

    monkeypatch.setattr("main.run_chatgpt_register_once", fake_run_chatgpt_register_once)
    monkeypatch.setattr("core.registration.get_management_key", lambda: "secret")  # type: ignore[no-untyped-def]
    monkeypatch.setattr("main.classify_token_file", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("readiness probe should not gate direct CPA sync")))  # type: ignore[no-untyped-def]
    monkeypatch.setattr("platforms.chatgpt.cpa_upload.upload_to_cpa", lambda *args, **kwargs: (False, "unexpected EOF"))  # type: ignore[no-untyped-def]

    loop._worker(thread_id=1, initial_provider="cfmail")

    payload = json.loads(pool_file.read_text(encoding="utf-8"))
    assert payload["backup_written"] is True
    assert payload["cpa_sync_status"] == "failed"
    assert payload["last_cpa_sync_error"] == "unexpected EOF"


def test_registration_loop_dumps_engine_logs_on_failure(monkeypatch) -> None:
    settings = _base_settings(register_max_consecutive_failures=1, register_mail_provider="mailtm")
    loop = RegistrationLoop(settings)
    loop._providers = ["mailtm"]
    logged: list[str] = []
    original_log = loop._log

    def capture_log(msg: str) -> None:
        logged.append(msg)

    loop._log = capture_log

    def fake_run_chatgpt_register_once(**kwargs):  # type: ignore[no-untyped-def]
        loop._stop_event.set()
        return {
            "success": False,
            "stage": "signup",
            "error_message": "HTTP 403: access denied",
            "logs": [
                "[09:35:20] check_ip_location: JP",
                "[09:35:21] created mailbox: test@example.com",
                "[09:35:22] signup form status: 403",
            ],
        }

    monkeypatch.setattr("main.run_chatgpt_register_once", fake_run_chatgpt_register_once)

    loop._worker(thread_id=1, initial_provider="mailtm")

    # Verify stage appears in the failure line
    fail_lines = [line for line in logged if "failed" in line and "stage=" in line]
    assert len(fail_lines) == 1
    assert "[stage=signup]" in fail_lines[0]
    assert "HTTP 403: access denied" in fail_lines[0]

    # Verify engine logs are dumped with ↳ prefix
    engine_lines = [line for line in logged if "\u21b3" in line]
    assert len(engine_lines) == 3
    assert "signup form status: 403" in engine_lines[2]
    snapshot = loop.snapshot()
    assert snapshot["failure_by_stage"]["signup"] == 1
    assert snapshot["recent_failure_hotspots"] == [{"key": "signup", "stage": "signup", "count": 1}]


def test_registration_loop_rotates_cfmail_domain_after_blacklist_threshold(monkeypatch) -> None:
    settings = _base_settings(register_mail_provider="cfmail", register_max_consecutive_failures=5)
    loop = RegistrationLoop(settings)
    loop._providers = ["cfmail"]
    loop._cfmail_tracker = DomainHealthTracker(window_size=2, blacklist_threshold=2, rotation_cooldown_seconds=1)
    rotation_calls: list[str] = []

    class FakeProvisioner:
        def rotate_active_domain(self):  # type: ignore[no-untyped-def]
            rotation_calls.append("rotate")
            return ProvisionResult(
                success=True,
                step="completed",
                old_domain="nova.example.test",
                new_domain="auto0322.example.test",
            )

    loop._cfmail_provisioner = FakeProvisioner()

    attempts = {"count": 0}

    def fake_run_chatgpt_register_once(**kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        attempts["count"] += 1
        if attempts["count"] >= 2:
            loop._stop_event.set()
        return {
            "success": False,
            "stage": "create_account",
            "error_message": "create account failed",
            "metadata": {
                "mail_provider": "cfmail",
                "email_domain": "nova.example.test",
                "create_account_error_code": "registration_disallowed",
                "create_account_error_message": "blocked",
            },
        }

    monkeypatch.setattr("main.run_chatgpt_register_once", fake_run_chatgpt_register_once)

    loop._worker(thread_id=1, initial_provider="cfmail")

    assert rotation_calls == ["rotate"]
    assert loop.snapshot()["cfmail_rotation"]["last_new_domain"] == "auto0322.example.test"


def test_registration_loop_does_not_rotate_cfmail_on_mailbox_failure(monkeypatch) -> None:
    settings = _base_settings(register_mail_provider="cfmail", register_max_consecutive_failures=2)
    loop = RegistrationLoop(settings)
    loop._providers = ["cfmail"]
    loop._cfmail_tracker = DomainHealthTracker(window_size=2, blacklist_threshold=2, rotation_cooldown_seconds=1)

    class FakeProvisioner:
        def rotate_active_domain(self):  # type: ignore[no-untyped-def]
            raise AssertionError("rotation should not be called")

    loop._cfmail_provisioner = FakeProvisioner()

    def fake_run_chatgpt_register_once(**kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        loop._stop_event.set()
        return {
            "success": False,
            "stage": "mailbox",
            "error_message": "create email failed",
            "metadata": {"mail_provider": "cfmail"},
        }

    monkeypatch.setattr("main.run_chatgpt_register_once", fake_run_chatgpt_register_once)

    loop._worker(thread_id=1, initial_provider="cfmail")

    assert loop.snapshot()["cfmail_rotation"]["last_new_domain"] == ""


def test_registration_loop_rotates_cfmail_on_invalid_domain_mailbox_failure() -> None:
    settings = _base_settings(register_mail_provider="cfmail")
    loop = RegistrationLoop(settings)
    loop._providers = ["cfmail"]
    loop._cfmail_tracker = DomainHealthTracker(window_size=2, blacklist_threshold=2, rotation_cooldown_seconds=1)

    class FakeProvisioner:
        def rotate_active_domain(self):  # type: ignore[no-untyped-def]
            return ProvisionResult(
                success=True,
                step="rotate",
                old_domain="bad.example.test",
                new_domain="auto-new.example.test",
            )

    reload_calls: list[bool] = []

    class FakeManager:
        def reload_if_needed(self, force=False):  # type: ignore[no-untyped-def]
            reload_calls.append(bool(force))
            return True

    loop._cfmail_manager = FakeManager()
    loop._cfmail_provisioner = FakeProvisioner()
    loop._cfmail_wait_otp_cooldown_seconds = 60
    loop._cfmail_add_phone_cooldown_seconds = 60

    result = {
        "success": False,
        "stage": "mailbox",
        "error_message": "create email failed",
        "logs": ["[10:00:00] create_email failed: 创建邮箱地址失败: 无效的域名"],
        "metadata": {
            "mail_provider": "cfmail",
            "email_domain": "bad.example.test",
        },
    }

    assert loop._force_rotate_cfmail_for_invalid_mailbox(thread_id=1, result=result) is True
    snapshot = loop.snapshot()
    assert snapshot["cfmail_rotation"]["last_new_domain"] == "auto-new.example.test"
    assert snapshot["cfmail_wait_otp_stoploss"]["active_domain"] == "auto-new.example.test"
    assert reload_calls == [True]


def test_registration_loop_startup_preflight_rotates_invalid_active_domain(monkeypatch) -> None:
    settings = _base_settings(register_mail_provider="cfmail")
    loop = RegistrationLoop(settings)
    loop._cfmail_tracker = DomainHealthTracker(window_size=2, blacklist_threshold=2, rotation_cooldown_seconds=1)

    class FakeMailbox:
        calls = 0

        def get_email(self):  # type: ignore[no-untyped-def]
            type(self).calls += 1
            raise RuntimeError("创建邮箱地址失败: 无效的域名")

    class FakeProvisioner:
        def rotate_active_domain(self):  # type: ignore[no-untyped-def]
            return ProvisionResult(
                success=True,
                step="rotate",
                old_domain="bad.example.test",
                new_domain="auto-new.example.test",
            )

    loop._cfmail_manager = object()
    loop._cfmail_provisioner = FakeProvisioner()
    monkeypatch.setattr(loop, "_current_cfmail_active_domain", lambda: "bad.example.test")
    monkeypatch.setattr("core.base_mailbox.create_mailbox", lambda provider, proxy=None: FakeMailbox())

    assert loop._ensure_cfmail_active_domain_ready() is True
    snapshot = loop.snapshot()
    assert snapshot["cfmail_rotation"]["last_new_domain"] == "auto-new.example.test"
    assert snapshot["cfmail_wait_otp_stoploss"]["active_domain"] == "auto-new.example.test"


def test_registration_loop_forces_cfmail_rotation_when_all_accounts_in_cooldown(monkeypatch) -> None:
    settings = _base_settings(register_mail_provider="cfmail", register_max_consecutive_failures=2)
    loop = RegistrationLoop(settings)
    loop._providers = ["cfmail"]
    loop._cfmail_tracker = DomainHealthTracker(window_size=2, blacklist_threshold=2, rotation_cooldown_seconds=1)
    run_calls: list[str] = []
    rotation_calls: list[str] = []

    class FakeManager:
        def reload_if_needed(self) -> bool:
            return False

        def select_account(self, profile_name=None):  # type: ignore[no-untyped-def]
            del profile_name
            return None

    class FakeProvisioner:
        def rotate_active_domain(self):  # type: ignore[no-untyped-def]
            rotation_calls.append("rotate")
            loop._stop_event.set()
            return ProvisionResult(
                success=True,
                step="completed",
                old_domain="auto-old.example.test",
                new_domain="auto-new.example.test",
            )

    def fake_run_chatgpt_register_once(**kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        loop._stop_event.set()
        run_calls.append("called")
        raise AssertionError("registration should not run while cfmail is fully unavailable")

    loop._cfmail_manager = FakeManager()
    loop._cfmail_provisioner = FakeProvisioner()
    monkeypatch.setattr("main.run_chatgpt_register_once", fake_run_chatgpt_register_once)

    loop._worker(thread_id=1, initial_provider="cfmail")

    assert rotation_calls == ["rotate"]
    assert run_calls == []
    assert loop.snapshot()["cfmail_rotation"]["last_new_domain"] == "auto-new.example.test"


def test_registration_loop_does_not_penalize_proxy_for_blacklist_failure(monkeypatch) -> None:
    settings = _base_settings(register_mail_provider="cfmail")
    loop = RegistrationLoop(settings)
    loop._providers = ["cfmail"]
    recorded: dict[str, object] = {}

    class FakeLease:
        name = "sg-node"
        local_port = 17891
        proxy_url = "socks5://127.0.0.1:17891"

    class FakePool:
        def acquire(self, timeout=5.0):  # type: ignore[no-untyped-def]
            return FakeLease()

        def release(self, lease, *, success, stage=None):  # type: ignore[no-untyped-def]
            recorded["released"] = (success, stage)

    def fake_run_chatgpt_register_once(**kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        loop._stop_event.set()
        return {
            "success": False,
            "stage": "create_account",
            "error_message": "create account failed",
            "metadata": {
                "mail_provider": "cfmail",
                "email_domain": "nova.example.test",
                "create_account_error_code": "registration_disallowed",
            },
        }

    loop._proxy_pool = FakePool()
    monkeypatch.setattr("main.run_chatgpt_register_once", fake_run_chatgpt_register_once)

    loop._worker(thread_id=1, initial_provider="cfmail")

    assert recorded["released"] == (None, "create_account")


def test_registration_loop_records_add_phone_gate_signal(monkeypatch) -> None:
    settings = _base_settings(register_mail_provider="cfmail")
    loop = RegistrationLoop(settings)
    loop._providers = ["cfmail"]

    def fake_run_chatgpt_register_once(**kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        loop._stop_event.set()
        return {
            "success": False,
            "stage": "add_phone_gate",
            "error_message": "post-create flow requires phone gate",
            "metadata": {
                "mail_provider": "cfmail",
                "email_domain": "demo.example.test",
                "post_create_gate": "add_phone",
            },
        }

    monkeypatch.setattr("main.run_chatgpt_register_once", fake_run_chatgpt_register_once)

    loop._worker(thread_id=1, initial_provider="cfmail")

    snapshot = loop.snapshot()
    assert snapshot["failure_by_stage"]["add_phone_gate"] == 1
    assert snapshot["failure_signals"]["add_phone_gate"] == 1
    assert snapshot["recent_failure_hotspots"] == [{"key": "add_phone_gate", "stage": "add_phone_gate", "count": 1}]
    assert snapshot["recent_attempts"][0]["post_create_gate"] == "add_phone"


def test_registration_loop_classifies_user_already_exists_as_mailbox_reused(monkeypatch) -> None:
    settings = _base_settings(register_mail_provider="cfmail")
    loop = RegistrationLoop(settings)
    loop._providers = ["cfmail"]

    def fake_run_chatgpt_register_once(**kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        loop._stop_event.set()
        return {
            "success": False,
            "stage": "create_account",
            "error_message": "create account failed",
            "email": "dup@example.com",
            "metadata": {
                "mail_provider": "cfmail",
                "email_domain": "demo.example.test",
                "create_account_error_code": "user_already_exists",
            },
        }

    monkeypatch.setattr("main.run_chatgpt_register_once", fake_run_chatgpt_register_once)

    loop._worker(thread_id=1, initial_provider="cfmail")

    snapshot = loop.snapshot()
    assert snapshot["failure_by_stage"]["create_account"] == 1
    assert snapshot["failure_signals"]["mailbox_reused"] == 1
    assert snapshot["recent_failure_hotspots"] == [{"key": "mailbox_reused", "stage": "create_account", "count": 1}]


def test_registration_loop_activates_add_phone_stoploss(monkeypatch) -> None:
    settings = _base_settings(register_mail_provider="cfmail")
    loop = RegistrationLoop(settings)
    loop._providers = ["cfmail"]
    monkeypatch.setattr(loop, "_current_cfmail_active_domain", lambda: "demo.example.test")
    loop._cfmail_add_phone_window = 2
    loop._cfmail_add_phone_threshold = 2
    loop._cfmail_add_phone_max_successes = 0
    loop._cfmail_add_phone_cooldown_seconds = 60

    attempts = {"count": 0}

    def fake_run_chatgpt_register_once(**kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        attempts["count"] += 1
        if attempts["count"] >= 2:
            loop._stop_event.set()
        return {
            "success": False,
            "stage": "add_phone_gate",
            "error_message": "post-create flow requires phone gate",
            "metadata": {
                "mail_provider": "cfmail",
                "email_domain": "demo.example.test",
                "post_create_gate": "add_phone",
            },
        }

    monkeypatch.setattr("main.run_chatgpt_register_once", fake_run_chatgpt_register_once)

    loop._worker(thread_id=1, initial_provider="cfmail")

    snapshot = loop.snapshot()
    stoploss = snapshot["cfmail_add_phone_stoploss"]
    assert stoploss["active_domain"] == "demo.example.test"
    assert stoploss["in_cooldown"] is True
    assert stoploss["last_add_phone_failures"] == 2
    assert stoploss["last_successes"] == 0


def test_registration_loop_snapshot_exposes_active_domain_only_attempts() -> None:
    settings = _base_settings(register_mail_provider="cfmail")
    loop = RegistrationLoop(settings)
    loop._recent_attempts.extend(
        [
            {
                "timestamp": "2026-03-24T18:00:00",
                "success": False,
                "stage": "create_account",
                "signal": "registration_disallowed",
                "error_message": "failed",
                "email_domain": "old.example.test",
                "proxy_key": "old-node",
                "email": "old@old.example.test",
            },
            {
                "timestamp": "2026-03-24T18:00:10",
                "success": True,
                "stage": "completed",
                "signal": "",
                "error_message": "",
                "email_domain": "new.example.test",
                "proxy_key": "new-node-1",
                "email": "ok@new.example.test",
            },
            {
                "timestamp": "2026-03-24T18:00:20",
                "success": False,
                "stage": "create_account",
                "signal": "registration_disallowed",
                "error_message": "failed",
                "email_domain": "new.example.test",
                "proxy_key": "new-node-2",
                "email": "bad@new.example.test",
            },
        ]
    )

    class FakeTracker:
        def snapshot(self) -> dict[str, object]:
            return {
                "active_domain": "new.example.test",
                "last_new_domain": "new.example.test",
                "last_blacklisted_domain": "old.example.test",
            }

    loop._cfmail_tracker = FakeTracker()

    snapshot = loop.snapshot()

    assert [item["email_domain"] for item in snapshot["active_domain_recent_attempts"]] == [
        "new.example.test",
        "new.example.test",
    ]
    assert snapshot["active_domain_failure_by_stage"] == {"create_account": 1}
    assert snapshot["active_domain_failure_signals"] == {"registration_disallowed": 1}
    assert snapshot["active_domain_recent_failure_hotspots"] == [
        {"key": "registration_disallowed", "stage": "create_account", "count": 1}
    ]


def test_registration_loop_snapshot_infers_active_domain_from_recent_attempts() -> None:
    settings = _base_settings(register_mail_provider="cfmail")
    loop = RegistrationLoop(settings)
    loop._recent_attempts.extend(
        [
            {
                "timestamp": "2026-03-24T18:10:00",
                "success": False,
                "stage": "create_account",
                "signal": "registration_disallowed",
                "error_message": "failed",
                "email_domain": "old.example.test",
                "proxy_key": "old-node",
                "email": "old@old.example.test",
            },
            {
                "timestamp": "2026-03-24T18:10:10",
                "success": True,
                "stage": "completed",
                "signal": "",
                "error_message": "",
                "email_domain": "new.example.test",
                "proxy_key": "new-node",
                "email": "ok@new.example.test",
            },
        ]
    )

    snapshot = loop.snapshot()

    assert [item["email_domain"] for item in snapshot["active_domain_recent_attempts"]] == [
        "new.example.test"
    ]


def test_registration_loop_disables_add_phone_cooldown_when_configured_zero(monkeypatch) -> None:
    monkeypatch.setenv("ZHUCE6_CFMAIL_ADD_PHONE_COOLDOWN_SECONDS", "0")
    settings = _base_settings(register_mail_provider="cfmail")
    loop = RegistrationLoop(settings)
    loop._cfmail_add_phone_window = 2
    loop._cfmail_add_phone_threshold = 2
    loop._cfmail_add_phone_max_successes = 0

    result = {
        "success": False,
        "stage": "add_phone_gate",
        "error_message": "post-create flow requires phone gate",
        "metadata": {
            "mail_provider": "cfmail",
            "email_domain": "demo.example.test",
            "post_create_gate": "add_phone",
        },
    }

    loop._update_cfmail_add_phone_stoploss(result)
    loop._update_cfmail_add_phone_stoploss(result)

    stoploss = loop.snapshot()["cfmail_add_phone_stoploss"]
    assert stoploss["in_cooldown"] is False
    assert stoploss["cooldown_remaining_seconds"] == 0


def test_registration_loop_activates_wait_otp_stoploss_for_no_message_timeouts(monkeypatch) -> None:
    settings = _base_settings(register_mail_provider="cfmail")
    loop = RegistrationLoop(settings)
    monkeypatch.setattr(loop, "_current_cfmail_active_domain", lambda: "demo.example.test")
    loop._cfmail_wait_otp_window = 2
    loop._cfmail_wait_otp_threshold = 2
    loop._cfmail_wait_otp_cooldown_seconds = 60

    result = {
        "success": False,
        "stage": "wait_otp",
        "error_message": "otp retrieval failed",
        "metadata": {
            "mail_provider": "cfmail",
            "email_domain": "demo.example.test",
            "otp_wait_failure_reason": "mailbox_timeout_no_message",
            "otp_mailbox_message_scan_count": 0,
        },
    }

    loop._update_cfmail_wait_otp_stoploss(result)
    loop._update_cfmail_wait_otp_stoploss(result)

    stoploss = loop.snapshot()["cfmail_wait_otp_stoploss"]
    assert stoploss["active_domain"] == "demo.example.test"
    assert stoploss["in_cooldown"] is True
    assert stoploss["last_no_message_timeouts"] == 2


def test_registration_loop_does_not_activate_wait_otp_stoploss_when_window_contains_success(monkeypatch) -> None:
    settings = _base_settings(register_mail_provider="cfmail")
    loop = RegistrationLoop(settings)
    monkeypatch.setattr(loop, "_current_cfmail_active_domain", lambda: "demo.example.test")
    loop._cfmail_wait_otp_window = 2
    loop._cfmail_wait_otp_threshold = 2
    loop._cfmail_wait_otp_cooldown_seconds = 60

    timeout_result = {
        "success": False,
        "stage": "wait_otp",
        "error_message": "otp retrieval failed",
        "metadata": {
            "mail_provider": "cfmail",
            "email_domain": "demo.example.test",
            "otp_wait_failure_reason": "mailbox_timeout_no_message",
            "otp_mailbox_message_scan_count": 0,
        },
    }
    success_result = {
        "success": True,
        "stage": "completed",
        "email": "ok@demo.example.test",
        "metadata": {
            "mail_provider": "cfmail",
            "email_domain": "demo.example.test",
        },
    }

    loop._update_cfmail_wait_otp_stoploss(timeout_result)
    loop._update_cfmail_wait_otp_stoploss(success_result)

    stoploss = loop.snapshot()["cfmail_wait_otp_stoploss"]
    assert stoploss["active_domain"] == "demo.example.test"
    assert stoploss["in_cooldown"] is False


def test_registration_loop_does_not_activate_wait_otp_stoploss_when_window_contains_message_seen(monkeypatch) -> None:
    settings = _base_settings(register_mail_provider="cfmail")
    loop = RegistrationLoop(settings)
    monkeypatch.setattr(loop, "_current_cfmail_active_domain", lambda: "demo.example.test")
    loop._cfmail_wait_otp_window = 2
    loop._cfmail_wait_otp_threshold = 2
    loop._cfmail_wait_otp_cooldown_seconds = 60

    timeout_result = {
        "success": False,
        "stage": "wait_otp",
        "error_message": "otp retrieval failed",
        "metadata": {
            "mail_provider": "cfmail",
            "email_domain": "demo.example.test",
            "otp_wait_failure_reason": "mailbox_timeout_no_message",
            "otp_mailbox_message_scan_count": 0,
        },
    }
    message_seen_result = {
        "success": False,
        "stage": "wait_otp",
        "error_message": "otp retrieval failed",
        "metadata": {
            "mail_provider": "cfmail",
            "email_domain": "demo.example.test",
            "otp_wait_failure_reason": "mailbox_timeout_no_match",
            "otp_mailbox_message_scan_count": 1,
        },
    }

    loop._update_cfmail_wait_otp_stoploss(timeout_result)
    loop._update_cfmail_wait_otp_stoploss(message_seen_result)

    stoploss = loop.snapshot()["cfmail_wait_otp_stoploss"]
    assert stoploss["active_domain"] == "demo.example.test"
    assert stoploss["in_cooldown"] is False


def test_registration_loop_blocks_non_owner_threads_while_cfmail_canary_pending() -> None:
    settings = _base_settings(register_mail_provider="cfmail")
    loop = RegistrationLoop(settings)
    loop._cfmail_canary_state.update(
        {
            "active_domain": "demo.example.test",
            "pending": True,
            "owner_thread_id": 1,
            "attempt_started_at": 9999999999.0,
            "last_logged_at": 0.0,
        }
    )

    assert loop._wait_if_cfmail_canary_pending(thread_id=2, provider="cfmail") is True
    assert loop._wait_if_cfmail_canary_pending(thread_id=1, provider="cfmail") is False


def test_registration_loop_marks_cfmail_canary_ready_after_first_message_seen(monkeypatch) -> None:
    settings = _base_settings(register_mail_provider="cfmail")
    loop = RegistrationLoop(settings)
    monkeypatch.setattr(loop, "_current_cfmail_active_domain", lambda: "demo.example.test")
    loop._cfmail_canary_state.update(
        {
            "active_domain": "demo.example.test",
            "pending": True,
            "owner_thread_id": 3,
            "attempt_started_at": 1743242400.0,
        }
    )

    result = {
        "success": False,
        "stage": "wait_otp",
        "error_message": "otp retrieval failed",
        "metadata": {
            "mail_provider": "cfmail",
            "email_domain": "demo.example.test",
            "otp_mailbox_message_scan_count": 1,
        },
    }

    loop._update_cfmail_canary_after_result(thread_id=3, result=result)

    assert loop.snapshot()["cfmail_canary"]["pending"] is False


def test_registration_loop_releases_cfmail_canary_owner_after_failed_attempt() -> None:
    settings = _base_settings(register_mail_provider="cfmail")
    loop = RegistrationLoop(settings)
    loop._current_cfmail_active_domain = lambda: "demo.example.test"  # type: ignore[method-assign]
    loop._cfmail_canary_state.update(
        {
            "active_domain": "demo.example.test",
            "pending": True,
            "owner_thread_id": 4,
            "attempt_started_at": 1743242400.0,
        }
    )

    result = {
        "success": False,
        "stage": "device_id",
        "error_message": "device id acquisition failed",
        "metadata": {
            "mail_provider": "cfmail",
            "email_domain": "demo.example.test",
            "otp_mailbox_message_scan_count": 0,
        },
    }

    loop._update_cfmail_canary_after_result(thread_id=4, result=result)

    assert loop.snapshot()["cfmail_canary"]["pending"] is True
    assert loop.snapshot()["cfmail_canary"]["owner_thread_id"] == 0


def test_registration_loop_rotates_failed_cfmail_canary_domain(monkeypatch) -> None:
    settings = _base_settings(register_mail_provider="cfmail")
    loop = RegistrationLoop(settings)
    monkeypatch.setattr(loop, "_current_cfmail_active_domain", lambda: "demo.example.test")
    loop._cfmail_tracker = DomainHealthTracker(window_size=2, blacklist_threshold=2, rotation_cooldown_seconds=1)
    loop._cfmail_canary_state.update(
        {
            "active_domain": "demo.example.test",
            "pending": True,
            "owner_thread_id": 4,
            "attempt_started_at": 1743242400.0,
        }
    )

    class FakeProvisioner:
        def rotate_active_domain(self):  # type: ignore[no-untyped-def]
            return ProvisionResult(
                success=True,
                step="rotate",
                old_domain="demo.example.test",
                new_domain="auto-new.example.test",
            )

    reload_calls: list[bool] = []

    class FakeManager:
        def reload_if_needed(self, force=False):  # type: ignore[no-untyped-def]
            reload_calls.append(bool(force))
            return True

    loop._cfmail_manager = FakeManager()
    loop._cfmail_provisioner = FakeProvisioner()
    result = {
        "success": False,
        "stage": "wait_otp",
        "error_message": "otp retrieval failed",
        "metadata": {
            "mail_provider": "cfmail",
            "email_domain": "demo.example.test",
            "otp_wait_failure_reason": "mailbox_timeout_no_message",
            "otp_mailbox_message_scan_count": 0,
        },
    }

    assert loop._rotate_cfmail_for_failed_canary(thread_id=4, result=result) is True

    snapshot = loop.snapshot()
    assert snapshot["cfmail_rotation"]["last_new_domain"] == "auto-new.example.test"
    assert snapshot["cfmail_canary"]["active_domain"] == "auto-new.example.test"
    assert snapshot["cfmail_canary"]["pending"] is True
    assert reload_calls == [True]


def test_registration_loop_tracks_fresh_domain_budget_after_mail_seen(monkeypatch) -> None:
    monkeypatch.setenv("ZHUCE6_CFMAIL_FRESH_DOMAIN_ATTEMPT_BUDGET", "2")
    settings = _base_settings(register_mail_provider="cfmail")
    loop = RegistrationLoop(settings)
    monkeypatch.setattr(loop, "_current_cfmail_active_domain", lambda: "demo.example.test")

    first_result = {
        "success": False,
        "stage": "add_phone_gate",
        "error_message": "post-create flow requires phone gate",
        "metadata": {
            "mail_provider": "cfmail",
            "email_domain": "demo.example.test",
            "otp_mailbox_message_scan_count": 1,
        },
    }
    second_result = {
        "success": False,
        "stage": "wait_otp",
        "error_message": "otp retrieval failed",
        "metadata": {
            "mail_provider": "cfmail",
            "email_domain": "demo.example.test",
            "otp_mailbox_message_scan_count": 0,
        },
    }

    loop._update_cfmail_fresh_domain_budget(first_result)
    snapshot = loop.snapshot()["cfmail_fresh_domain_budget"]
    assert snapshot["completed_attempts"] == 1
    assert snapshot["mail_seen_attempts"] == 1
    assert snapshot["last_triggered_at"] == ""

    loop._update_cfmail_fresh_domain_budget(second_result)
    snapshot = loop.snapshot()["cfmail_fresh_domain_budget"]
    assert snapshot["completed_attempts"] == 2
    assert snapshot["mail_seen_attempts"] == 1
    assert snapshot["last_reason"] == "fresh_domain_attempt_budget_reached"


def test_registration_loop_rotates_domain_when_fresh_domain_budget_reached(monkeypatch) -> None:
    monkeypatch.setenv("ZHUCE6_CFMAIL_FRESH_DOMAIN_ATTEMPT_BUDGET", "2")
    settings = _base_settings(register_mail_provider="cfmail")
    loop = RegistrationLoop(settings)
    monkeypatch.setattr(loop, "_current_cfmail_active_domain", lambda: "demo.example.test")
    loop._cfmail_tracker = DomainHealthTracker(window_size=2, blacklist_threshold=2, rotation_cooldown_seconds=1)
    loop._cfmail_fresh_domain_state.update(
        {
            "active_domain": "demo.example.test",
            "completed_attempts": 2,
            "mail_seen_attempts": 1,
            "successes": 0,
            "last_triggered_at": "2026-03-29T10:00:00",
            "last_rotation_attempted_at": "",
            "last_reason": "fresh_domain_attempt_budget_reached",
        }
    )

    class FakeProvisioner:
        def rotate_active_domain(self):  # type: ignore[no-untyped-def]
            return ProvisionResult(
                success=True,
                step="rotate",
                old_domain="demo.example.test",
                new_domain="auto-new.example.test",
            )

    reload_calls: list[bool] = []

    class FakeManager:
        def reload_if_needed(self, force=False):  # type: ignore[no-untyped-def]
            reload_calls.append(bool(force))
            return True

    loop._cfmail_manager = FakeManager()
    loop._cfmail_provisioner = FakeProvisioner()

    assert loop._rotate_cfmail_for_fresh_domain_budget(thread_id=5) is True

    snapshot = loop.snapshot()
    assert snapshot["cfmail_rotation"]["last_new_domain"] == "auto-new.example.test"
    assert snapshot["cfmail_fresh_domain_budget"]["active_domain"] == "auto-new.example.test"
    assert snapshot["cfmail_fresh_domain_budget"]["completed_attempts"] == 0
    assert snapshot["cfmail_canary"]["active_domain"] == "auto-new.example.test"
    assert reload_calls == [True]


def test_registration_loop_throttles_cfmail_inflight_attempts(monkeypatch) -> None:
    monkeypatch.setenv("ZHUCE6_CFMAIL_MAX_INFLIGHT", "1")
    monkeypatch.setenv("ZHUCE6_CFMAIL_START_INTERVAL_SECONDS", "0")
    settings = _base_settings(register_mail_provider="cfmail")
    loop = RegistrationLoop(settings)
    monkeypatch.setattr(loop, "_current_cfmail_active_domain", lambda: "demo.example.test")

    assert loop._wait_if_cfmail_flow_throttled(thread_id=1, provider="cfmail") is False
    assert loop._wait_if_cfmail_flow_throttled(thread_id=2, provider="cfmail") is True

    loop._release_cfmail_flow_slot(thread_id=1)

    assert loop._wait_if_cfmail_flow_throttled(thread_id=2, provider="cfmail") is False


def test_registration_loop_throttles_cfmail_start_interval(monkeypatch) -> None:
    monkeypatch.setenv("ZHUCE6_CFMAIL_MAX_INFLIGHT", "2")
    monkeypatch.setenv("ZHUCE6_CFMAIL_START_INTERVAL_SECONDS", "15")
    settings = _base_settings(register_mail_provider="cfmail")
    loop = RegistrationLoop(settings)
    monkeypatch.setattr(loop, "_current_cfmail_active_domain", lambda: "demo.example.test")
    current_time = {"value": 100.0}
    monkeypatch.setattr("core.registration.time.time", lambda: current_time["value"])

    assert loop._wait_if_cfmail_flow_throttled(thread_id=1, provider="cfmail") is False

    loop._release_cfmail_flow_slot(thread_id=1)
    current_time["value"] = 105.0
    assert loop._wait_if_cfmail_flow_throttled(thread_id=2, provider="cfmail") is True

    current_time["value"] = 116.0
    assert loop._wait_if_cfmail_flow_throttled(thread_id=2, provider="cfmail") is False


def test_registration_loop_activates_live_wait_otp_stoploss_on_stalled_waits(monkeypatch) -> None:
    settings = _base_settings(register_mail_provider="cfmail")
    loop = RegistrationLoop(settings)
    monkeypatch.setattr(loop, "_current_cfmail_active_domain", lambda: "demo.example.test")
    loop._cfmail_wait_otp_cooldown_seconds = 60
    loop._cfmail_wait_otp_live_threshold = 2
    loop._cfmail_wait_otp_live_age_seconds = 30

    account_a = MailboxAccount(
        email="a@demo.example.test",
        account_id="jwt-a",
        extra={"email_domain": "demo.example.test"},
    )
    account_b = MailboxAccount(
        email="b@demo.example.test",
        account_id="jwt-b",
        extra={"email_domain": "demo.example.test"},
    )

    loop._on_cfmail_wait_progress(
        account_a,
        {"message_scan_count": 0, "elapsed_seconds": 35},
    )
    loop._on_cfmail_wait_progress(
        account_b,
        {"message_scan_count": 0, "elapsed_seconds": 36},
    )

    stoploss = loop.snapshot()["cfmail_wait_otp_stoploss"]
    assert stoploss["active_domain"] == "demo.example.test"
    assert stoploss["in_cooldown"] is True
    assert stoploss["last_reason"] == "live wait_otp no-message threshold reached"
    assert stoploss["last_no_message_timeouts"] == 2


def test_registration_loop_live_wait_otp_stoploss_can_be_disabled(monkeypatch) -> None:
    settings = _base_settings(register_mail_provider="cfmail")
    loop = RegistrationLoop(settings)
    monkeypatch.setattr(loop, "_current_cfmail_active_domain", lambda: "demo.example.test")
    loop._cfmail_wait_otp_cooldown_seconds = 60
    loop._cfmail_wait_otp_live_threshold = 0
    loop._cfmail_wait_otp_live_age_seconds = 30

    account_a = MailboxAccount(
        email="a@demo.example.test",
        account_id="jwt-a",
        extra={"email_domain": "demo.example.test"},
    )
    account_b = MailboxAccount(
        email="b@demo.example.test",
        account_id="jwt-b",
        extra={"email_domain": "demo.example.test"},
    )

    loop._on_cfmail_wait_progress(
        account_a,
        {"message_scan_count": 0, "elapsed_seconds": 35},
    )
    loop._on_cfmail_wait_progress(
        account_b,
        {"message_scan_count": 0, "elapsed_seconds": 36},
    )

    stoploss = loop.snapshot()["cfmail_wait_otp_stoploss"]
    assert stoploss["in_cooldown"] is False


def test_registration_loop_does_not_activate_wait_otp_stoploss_for_non_mailbox_timeout() -> None:
    settings = _base_settings(register_mail_provider="cfmail")
    loop = RegistrationLoop(settings)
    loop._cfmail_wait_otp_window = 2
    loop._cfmail_wait_otp_threshold = 2
    loop._cfmail_wait_otp_cooldown_seconds = 60

    result = {
        "success": False,
        "stage": "wait_otp",
        "error_message": "otp retrieval failed",
        "metadata": {
            "mail_provider": "cfmail",
            "email_domain": "demo.example.test",
            "otp_wait_failure_reason": "mailbox_timeout_no_match",
            "otp_mailbox_message_scan_count": 1,
        },
    }

    loop._update_cfmail_wait_otp_stoploss(result)
    loop._update_cfmail_wait_otp_stoploss(result)

    stoploss = loop.snapshot()["cfmail_wait_otp_stoploss"]
    assert stoploss["in_cooldown"] is False


def test_registration_loop_rotates_cfmail_domain_when_wait_otp_stoploss_active(monkeypatch) -> None:
    settings = _base_settings(register_mail_provider="cfmail")
    loop = RegistrationLoop(settings)
    loop._providers = ["cfmail"]
    monkeypatch.setattr(loop, "_current_cfmail_active_domain", lambda: "demo.example.test")
    loop._cfmail_tracker = DomainHealthTracker(window_size=2, blacklist_threshold=2, rotation_cooldown_seconds=1)
    loop._cfmail_wait_otp_cooldown_seconds = 60
    loop._cfmail_wait_otp_state.update(
        {
            "active_domain": "demo.example.test",
            "in_cooldown": True,
            "cooldown_until": 9999999999.0,
            "last_triggered_at": "2026-03-29T10:00:00",
            "last_rotation_attempted_at": "",
        }
    )

    class FakeProvisioner:
        def rotate_active_domain(self):  # type: ignore[no-untyped-def]
            return ProvisionResult(
                success=True,
                step="rotate",
                old_domain="demo.example.test",
                new_domain="auto-new.example.test",
            )

    reload_calls: list[bool] = []

    class FakeManager:
        def reload_if_needed(self, force=False):  # type: ignore[no-untyped-def]
            reload_calls.append(bool(force))
            return True

    loop._cfmail_manager = FakeManager()
    loop._cfmail_provisioner = FakeProvisioner()

    assert loop._wait_if_cfmail_wait_otp_stopped(thread_id=1, provider="cfmail") is True

    snapshot = loop.snapshot()
    assert snapshot["cfmail_rotation"]["last_new_domain"] == "auto-new.example.test"
    assert snapshot["cfmail_wait_otp_stoploss"]["in_cooldown"] is False
    assert snapshot["cfmail_wait_otp_stoploss"]["active_domain"] == "auto-new.example.test"
    assert reload_calls == [True]


def test_registration_loop_rotates_cfmail_domain_when_add_phone_stoploss_active(monkeypatch) -> None:
    settings = _base_settings(register_mail_provider="cfmail")
    loop = RegistrationLoop(settings)
    loop._providers = ["cfmail"]
    monkeypatch.setattr(loop, "_current_cfmail_active_domain", lambda: "demo.example.test")
    loop._cfmail_tracker = DomainHealthTracker(window_size=2, blacklist_threshold=2, rotation_cooldown_seconds=1)
    loop._cfmail_add_phone_cooldown_seconds = 60
    loop._cfmail_add_phone_state.update(
        {
            "active_domain": "demo.example.test",
            "in_cooldown": True,
            "cooldown_until": 9999999999.0,
            "last_triggered_at": "2026-03-29T10:00:00",
            "last_rotation_attempted_at": "",
        }
    )

    class FakeProvisioner:
        def rotate_active_domain(self):  # type: ignore[no-untyped-def]
            return ProvisionResult(
                success=True,
                step="rotate",
                old_domain="demo.example.test",
                new_domain="auto-new.example.test",
            )

    loop._cfmail_provisioner = FakeProvisioner()

    assert loop._wait_if_cfmail_add_phone_stopped(thread_id=1, provider="cfmail") is True

    snapshot = loop.snapshot()
    assert snapshot["cfmail_rotation"]["last_new_domain"] == "auto-new.example.test"
    assert snapshot["cfmail_add_phone_stoploss"]["in_cooldown"] is False
    assert snapshot["cfmail_add_phone_stoploss"]["active_domain"] == "auto-new.example.test"


def test_registration_loop_ignores_stale_wait_otp_result_from_old_domain(monkeypatch) -> None:
    settings = _base_settings(register_mail_provider="cfmail")
    loop = RegistrationLoop(settings)
    monkeypatch.setattr(loop, "_current_cfmail_active_domain", lambda: "auto-new.example.test")

    result = {
        "success": False,
        "stage": "wait_otp",
        "error_message": "otp retrieval failed",
        "metadata": {
            "mail_provider": "cfmail",
            "email_domain": "auto-old.example.test",
            "otp_wait_failure_reason": "mailbox_timeout_no_message",
            "otp_mailbox_message_scan_count": 0,
        },
    }

    loop._update_cfmail_wait_otp_stoploss(result)

    stoploss = loop.snapshot()["cfmail_wait_otp_stoploss"]
    assert stoploss["active_domain"] == ""
    assert stoploss["in_cooldown"] is False


def test_registration_loop_does_not_abort_old_domain_wait_after_rotation(monkeypatch) -> None:
    settings = _base_settings(register_mail_provider="cfmail")
    loop = RegistrationLoop(settings)
    monkeypatch.setattr(loop, "_current_cfmail_active_domain", lambda: "auto-new.example.test")
    loop._cfmail_wait_otp_state.update(
        {
            "active_domain": "auto-old.example.test",
            "in_cooldown": True,
            "cooldown_until": 9999999999.0,
            "last_triggered_at": "2026-03-29T10:00:00",
        }
    )

    account = MailboxAccount(
        email="a@auto-old.example.test",
        account_id="jwt-old",
        extra={
            "email_domain": "auto-old.example.test",
            "otp_wait_started_at": 1743242390.0,
        },
    )

    assert loop._should_abort_cfmail_wait(account) is False


def test_registration_loop_aborts_wait_for_domain_in_wait_otp_cooldown(monkeypatch) -> None:
    settings = _base_settings(register_mail_provider="cfmail")
    loop = RegistrationLoop(settings)
    monkeypatch.setattr(loop, "_current_cfmail_active_domain", lambda: "demo.example.test")
    loop._cfmail_wait_otp_state.update(
        {
            "active_domain": "demo.example.test",
            "in_cooldown": True,
            "cooldown_until": 9999999999.0,
        }
    )

    account = MailboxAccount(
        email="a@demo.example.test",
        account_id="jwt-demo",
        extra={
            "email_domain": "demo.example.test",
            "otp_wait_started_at": 1743242410.0,
        },
    )

    assert loop._should_abort_cfmail_wait(account) is True


def test_registration_burst_scheduler_runs_batch_with_batch_config(monkeypatch, tmp_path: Path) -> None:
    settings = _base_settings(
        register_log_file="",
        runtime_state_file=tmp_path / "runtime_state.json",
        register_batch_threads=1,
        register_batch_target_count=20,
        register_batch_interval_seconds=60,
    )
    observed: dict[str, int] = {}

    class FakeLoop:
        def __init__(self, batch_settings):  # type: ignore[no-untyped-def]
            observed["threads"] = batch_settings.register_threads
            observed["target"] = batch_settings.register_target_count
            self._threads = []

        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

        def snapshot(self) -> dict[str, object]:
            return {
                "name": "register",
                "status": "stopped",
                "threads_alive": 0,
                "threads_total": 1,
                "total_attempts": 22,
                "total_success": 20,
                "total_failure": 2,
                "success_rate": 90.9,
                "target_count": 20,
                "target_reached": True,
                "last_error": "",
                "proxy": None,
                "proxy_pool_enabled": False,
                "mail_provider": "mailtm",
                "interval_seconds": 5,
                "run_count": 22,
                "success_count": 20,
                "failure_count": 2,
                "is_running": False,
                "last_started_at": None,
                "last_finished_at": None,
                "last_duration_seconds": None,
                "next_run_at": None,
                "failure_by_stage": {"add_phone_gate": 2},
                "failure_signals": {"add_phone_gate": 2},
                "recent_failure_hotspots": [{"key": "add_phone_gate", "stage": "add_phone_gate", "count": 2}],
                "recent_attempts": [
                    {
                        "timestamp": "2026-03-27T10:00:00",
                        "success": False,
                        "stage": "add_phone_gate",
                        "signal": "add_phone_gate",
                        "error_message": "phone gate",
                        "email_domain": "demo.example.test",
                        "post_create_gate": "add_phone",
                        "create_account_error_code": "",
                        "proxy_key": "",
                        "email": "",
                    }
                ],
                "active_domain_recent_attempts": [],
                "active_domain_failure_by_stage": {"add_phone_gate": 2},
                "active_domain_failure_signals": {"add_phone_gate": 2},
                "active_domain_recent_failure_hotspots": [{"key": "add_phone_gate", "stage": "add_phone_gate", "count": 2}],
                "cfmail_rotation": None,
                "cfmail_add_phone_stoploss": {"active_domain": "demo.example.test"}
            }

    monkeypatch.setattr("main.RegistrationLoop", FakeLoop)

    scheduler = RegistrationBurstScheduler(settings)
    original_absorb = scheduler._absorb_batch_snapshot

    def absorb_and_stop(snapshot: dict[str, object], *, duration_seconds: float) -> None:
        original_absorb(snapshot, duration_seconds=duration_seconds)
        scheduler.stop()

    monkeypatch.setattr(scheduler, "_absorb_batch_snapshot", absorb_and_stop)

    scheduler.run()

    payload = json.loads((tmp_path / "runtime_state.json").read_text(encoding="utf-8"))
    register_snapshot = payload["register_snapshot"]

    assert observed == {"threads": 1, "target": 20}
    assert register_snapshot["status"] == "stopped"
    assert register_snapshot["scheduler_mode"] == "burst"
    assert register_snapshot["run_count"] == 1
    assert register_snapshot["total_success"] == 20
    assert register_snapshot["batch_target_count"] == 20

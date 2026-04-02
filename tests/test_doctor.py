from __future__ import annotations

from pathlib import Path

from core.doctor import DoctorCheck, apply_doctor_fixes, collect_doctor_report, format_doctor_report
from core.settings import AppSettings


def test_collect_doctor_report_marks_lite_true_and_full_false(monkeypatch, tmp_path):
    settings = AppSettings.from_env()
    settings = AppSettings(
        **{
            **settings.__dict__,
            "env_file": tmp_path / ".env",
            "config_dir": tmp_path / "config",
            "state_dir": tmp_path / "state",
            "log_dir": tmp_path / "logs",
            "pool_dir": tmp_path / "pool",
            "backend": "cpa",
        }
    )
    settings.env_file.write_text("ZHUCE6_REGISTER_MAIL_PROVIDER=cfmail\n", encoding="utf-8")

    monkeypatch.setattr("core.doctor._check_python_version", lambda _settings: DoctorCheck("python", "ok", "Python 版本满足要求"))
    monkeypatch.setattr("core.doctor._check_env_file", lambda _settings: DoctorCheck("env", "ok", ".env 可读取"))
    monkeypatch.setattr("core.doctor._check_core_dependencies", lambda _settings: DoctorCheck("deps", "ok", "核心依赖齐全"))
    monkeypatch.setattr("core.doctor._check_cfmail", lambda _settings: DoctorCheck("cfmail", "ok", "cfmail 配置正常"))
    monkeypatch.setattr("core.doctor._check_proxy", lambda _settings: DoctorCheck("proxy", "ok", "代理可用"))
    monkeypatch.setattr("core.doctor._check_directory_writable", lambda _settings: DoctorCheck("dirs", "ok", "目录可写"))
    monkeypatch.setattr("core.doctor._check_sslocal", lambda _settings: DoctorCheck("sslocal", "skip", "使用 direct proxy URLs, 不依赖 sslocal", required_for=()))
    monkeypatch.setattr("core.doctor._check_cpa_management", lambda _settings: DoctorCheck("cpa", "error", "CPA management 不可达"))
    monkeypatch.setattr("core.doctor._check_sub2api", lambda _settings: DoctorCheck("sub2api", "skip", "当前 backend 不是 sub2api", required_for=()))

    report = collect_doctor_report(settings)

    assert report.lite_available is True
    assert report.full_available is False
    assert report.full_cpa_available is False
    assert report.full_sub2api_available is False
    text = format_doctor_report(report)
    assert "lite: available" in text
    assert "full(cpa): unavailable" in text
    assert "docker" not in text


def test_collect_doctor_report_marks_sub2api_available(monkeypatch, tmp_path):
    settings = AppSettings(
        env_file=tmp_path / ".env",
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        log_dir=tmp_path / "logs",
        pool_dir=tmp_path / "pool",
        backend="sub2api",
    )
    settings.env_file.write_text("ZHUCE6_REGISTER_MAIL_PROVIDER=mailtm\n", encoding="utf-8")

    monkeypatch.setattr("core.doctor._check_python_version", lambda _settings: DoctorCheck("python", "ok", "Python 版本满足要求"))
    monkeypatch.setattr("core.doctor._check_env_file", lambda _settings: DoctorCheck("env", "ok", ".env 可读取"))
    monkeypatch.setattr("core.doctor._check_core_dependencies", lambda _settings: DoctorCheck("deps", "ok", "核心依赖齐全"))
    monkeypatch.setattr("core.doctor._check_cfmail", lambda _settings: DoctorCheck("cfmail", "skip", "register 未启用 cfmail", required_for=()))
    monkeypatch.setattr("core.doctor._check_proxy", lambda _settings: DoctorCheck("proxy", "ok", "代理可用"))
    monkeypatch.setattr("core.doctor._check_directory_writable", lambda _settings: DoctorCheck("dirs", "ok", "目录可写"))
    monkeypatch.setattr("core.doctor._check_sslocal", lambda _settings: DoctorCheck("sslocal", "skip", "使用 direct proxy URLs, 不依赖 sslocal", required_for=()))
    monkeypatch.setattr("core.doctor._check_cpa_management", lambda _settings: DoctorCheck("cpa", "skip", "当前 backend 不是 cpa", required_for=()))
    monkeypatch.setattr("core.doctor._check_sub2api", lambda _settings: DoctorCheck("sub2api", "ok", "sub2api 可达"))

    report = collect_doctor_report(settings)

    assert report.lite_available is True
    assert report.full_available is True
    assert report.full_sub2api_available is True
    assert report.full_cpa_available is False


def test_check_sslocal_missing_in_config_mode_includes_install_guide(tmp_path):
    from core.doctor import _check_sslocal
    import core.doctor as doctor

    settings = AppSettings(proxy_pool_direct_urls="", proxy_pool_config=tmp_path / "clash.yaml")
    original_which = doctor.shutil.which
    doctor.shutil.which = lambda _name: None
    try:
        check = _check_sslocal(settings)
    finally:
        doctor.shutil.which = original_which

    assert check.status == "error"
    assert "未安装" in check.summary
    assert "shadowsocks-rust" in check.detail
    assert "Windows:" in check.detail


def test_format_doctor_report_renders_multiline_detail(tmp_path):
    settings = AppSettings(env_file=tmp_path / ".env")
    report = type(
        "FakeReport",
        (),
        {
            "settings": settings,
            "checks": (
                DoctorCheck("sslocal", "error", "未安装 sslocal", detail="Line A\nLine B"),
            ),
            "lite_available": False,
            "full_available": False,
            "full_cpa_available": False,
            "full_sub2api_available": False,
        },
    )()

    text = format_doctor_report(report)

    assert "Line A" in text
    assert "Line B" in text
    assert "full(sub2api): unavailable" in text


def test_check_proxy_requires_socksio_when_socks_proxy_configured(monkeypatch):
    from core.doctor import _check_proxy

    settings = AppSettings(register_proxy="socks5://127.0.0.1:1080", proxy_pool_direct_urls="", proxy_pool_config=None)
    original_import_module = __import__("importlib").import_module

    def fake_import_module(name: str, package=None):  # type: ignore[no-untyped-def]
        if name == "socksio":
            raise ModuleNotFoundError("No module named 'socksio'")
        return original_import_module(name, package)

    monkeypatch.setattr("core.doctor.importlib.import_module", fake_import_module)

    check = _check_proxy(settings)

    assert check.status == "error"
    assert "SOCKS" in check.summary
    assert "socksio" in check.detail
    assert "uv sync" in check.detail


def test_check_core_dependencies_reports_new_required_modules(monkeypatch):
    from core.doctor import _check_core_dependencies

    original_import_module = __import__("importlib").import_module

    def fake_import_module(name: str, package=None):  # type: ignore[no-untyped-def]
        if name in {"filelock", "psutil", "socksio"}:
            raise ModuleNotFoundError(f"No module named {name}")
        return original_import_module(name, package)

    monkeypatch.setattr("core.doctor.importlib.import_module", fake_import_module)

    check = _check_core_dependencies(AppSettings())

    assert check.status == "error"
    assert "filelock" in check.summary
    assert "psutil" in check.summary
    assert "socksio" in check.summary


def test_apply_doctor_fixes_runs_uv_sync_and_optional_npm(monkeypatch, tmp_path: Path):
    commands: list[tuple[tuple[str, ...], Path]] = []
    repo_root = tmp_path
    worker_dir = repo_root / "vendor" / "cfmail-worker" / "worker"
    worker_dir.mkdir(parents=True)
    (worker_dir / "package.json").write_text("{}", encoding="utf-8")

    def fake_run(args, cwd, check):  # type: ignore[no-untyped-def]
        commands.append((tuple(args), Path(cwd)))
        return None

    monkeypatch.setattr("core.doctor.subprocess.run", fake_run)

    settings = AppSettings(project_root=repo_root)
    actions = apply_doctor_fixes(settings)

    assert commands == [
        (("uv", "sync"), repo_root),
        (("npm", "install", "--no-fund", "--no-audit"), worker_dir),
    ]
    assert actions == [f"uv sync @ {repo_root}", f"npm install @ {worker_dir}"]

from __future__ import annotations

import pytest

import main


def test_build_arg_parser_supports_init_and_doctor():
    parser = main.build_arg_parser()

    args = parser.parse_args(["doctor"])
    assert args.command == "doctor"
    assert args.fix is False

    args = parser.parse_args(["init"])
    assert args.command == "init"


def test_build_arg_parser_supports_doctor_fix_flag():
    parser = main.build_arg_parser()

    args = parser.parse_args(["doctor", "--fix"])

    assert args.command == "doctor"
    assert args.fix is True


def test_main_doctor_command_prints_report(monkeypatch, capsys):
    monkeypatch.setattr(main.process_manager, "stop_all", lambda: [])

    class FakeReport:
        lite_available = True
        full_available = False

    monkeypatch.setattr(main, "collect_doctor_report", lambda: FakeReport())
    monkeypatch.setattr(main, "format_doctor_report", lambda report: "lite: available\nfull: unavailable")

    main.main(["doctor"])

    output = capsys.readouterr().out
    assert "lite: available" in output
    assert "full: unavailable" in output


def test_main_init_command_runs_setup_wizard(monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(main, "run_setup_wizard", lambda: called.append("wizard"))
    monkeypatch.setattr(main, "_run_uv_sync", lambda: called.append("uv-sync") or True)

    main.main(["init"])

    assert called == ["wizard", "uv-sync"]

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_legacy_start_scripts_removed() -> None:
    project_root = Path("/home/sophomores/zhuce6")
    assert not (project_root / "scripts/start_register_loop.sh").exists()
    assert not (project_root / "scripts/start_register_burst_scheduler.sh").exists()
    assert not (project_root / "scripts/start_dashboard.sh").exists()
    assert not (project_root / "scripts/start_full_stack.sh").exists()
    assert not (project_root / "scripts/_common.sh").exists()
    assert not (project_root / "start_register.sh").exists()


def test_main_module_bootstraps_env_file_on_import(tmp_path: Path) -> None:
    env_file = tmp_path / "test.env"
    env_file.write_text("ZHUCE6_TEST_VAR=hello\n", encoding="utf-8")
    env = os.environ.copy()
    env["ZHUCE6_ENV_FILE"] = str(env_file)
    env["PYTHONPATH"] = "/home/sophomores/zhuce6"

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os, sys; "
                "sys.argv=['main.py','status']; "
                "import main; "
                "print(os.environ.get('ZHUCE6_TEST_VAR'))"
            ),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )

    assert result.stdout.strip().endswith("hello")

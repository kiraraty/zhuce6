from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.settings import AppSettings
from ops.responses_survival import run_responses_survival_loop


def main() -> int:
    settings = AppSettings.from_env()
    run_responses_survival_loop(
        pool_dir=settings.pool_dir,
        state_file=settings.responses_survival_state_file,
        cohort_size=8,
        proxy=settings.account_survival_proxy,
        timeout_seconds=max(20, int(settings.account_survival_timeout_seconds)),
        interval_seconds=60,
        reseed=True,
        max_rounds=0,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Shared path resolution for zhuce6."""

from __future__ import annotations

import os
from pathlib import Path


def _resolve_env_path(name: str, default: Path) -> Path:
    raw = str(os.getenv(name, "")).strip()
    return Path(raw).expanduser().resolve() if raw else default.expanduser().resolve()


PROJECT_ROOT = _resolve_env_path("ZHUCE6_PROJECT_ROOT", Path(__file__).resolve().parents[1])
CONFIG_DIR = _resolve_env_path("ZHUCE6_CONFIG_DIR", PROJECT_ROOT / "config")
STATE_DIR = _resolve_env_path("ZHUCE6_STATE_DIR", PROJECT_ROOT / "state")
LOG_DIR = _resolve_env_path("ZHUCE6_LOG_DIR", PROJECT_ROOT / "logs")

DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_RUNTIME_STATE_FILE = STATE_DIR / "runtime_state.json"
DEFAULT_ACCOUNT_SURVIVAL_STATE_FILE = STATE_DIR / "account_survival_tracker.json"
DEFAULT_RESPONSES_SURVIVAL_STATE_FILE = STATE_DIR / "responses_survival_tracker.json"
DEFAULT_CFMAIL_CONFIG_PATH = CONFIG_DIR / "cfmail_accounts.json"
DEFAULT_DASHBOARD_LOG_FILE = LOG_DIR / "dashboard.log"
DEFAULT_REGISTER_LOG_FILE = LOG_DIR / "register.log"


def resolve_cfmail_config_path() -> Path:
    explicit = str(os.getenv("ZHUCE6_CFMAIL_CONFIG_PATH", "")).strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    return DEFAULT_CFMAIL_CONFIG_PATH

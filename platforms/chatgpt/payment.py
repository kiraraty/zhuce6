"""Payment-related helpers for the zhuce6 ChatGPT platform."""

from __future__ import annotations

from typing import Any


def check_subscription_status(account: Any, proxy: str | None = None) -> str | None:
    del proxy
    access_token = str(getattr(account, "access_token", "") or "").strip()
    if not access_token:
        return None
    return "unknown"


def generate_plus_link(account: Any, proxy: str | None = None, country: str = "US") -> str:
    del account, proxy
    return f"https://chatgpt.com/#pricing?plan=plus&country={country}"


def generate_team_link(account: Any, proxy: str | None = None, country: str = "US") -> str:
    del account, proxy
    return f"https://chatgpt.com/#pricing?plan=team&country={country}"

"""Reusable ChatGPT flow helpers for API routes and standalone scripts."""

from __future__ import annotations

import json
from pathlib import Path

from core.base_platform import RegisterConfig
from core.registry import get, load_all


def run_chatgpt_preflight(
    *,
    email: str | None,
    password: str | None,
    mail_provider: str,
    proxy: str | None,
) -> dict[str, object]:
    load_all()
    platform_cls = get("chatgpt")
    platform = platform_cls(
        config=RegisterConfig(
            proxy=proxy,
            extra={"mail_provider": mail_provider},
        )
    )
    return platform.run_preflight(email=email, password=password)


def run_chatgpt_register_once(
    *,
    email: str | None,
    password: str | None,
    mail_provider: str,
    proxy: str | None,
    write_pool: bool,
    pool_dir: Path,
) -> dict[str, object]:
    load_all()
    platform_cls = get("chatgpt")
    platform = platform_cls(
        config=RegisterConfig(
            proxy=proxy,
            extra={"mail_provider": mail_provider},
        )
    )
    return platform.run_register_once(
        email=email,
        password=password,
        write_pool=write_pool,
        pool_dir=pool_dir,
    )


def run_chatgpt_callback_exchange(
    *,
    callback_url: str,
    expected_state: str,
    code_verifier: str,
    proxy: str | None,
    write_pool: bool,
    pool_dir: Path,
) -> dict[str, object]:
    load_all()
    platform_cls = get("chatgpt")
    platform = platform_cls(config=RegisterConfig(proxy=proxy))
    return platform.exchange_callback(
        callback_url=callback_url,
        expected_state=expected_state,
        code_verifier=code_verifier,
        write_pool=write_pool,
        pool_dir=pool_dir,
    )


def print_preflight_summary(payload: dict[str, object]) -> None:
    print(f"success: {payload.get('success')}")
    print(f"stage: {payload.get('stage')}")
    print(f"email: {payload.get('email') or '-'}")
    print(f"error_message: {payload.get('error_message') or '-'}")
    metadata = payload.get("metadata") or {}
    if isinstance(metadata, dict):
        print(f"oauth_url: {metadata.get('oauth_url') or '-'}")
        print(f"mail_provider: {metadata.get('mail_provider') or '-'}")
    logs = payload.get("logs") or []
    if isinstance(logs, list) and logs:
        print("logs:")
        for line in logs:
            print(f"  {line}")


def print_callback_summary(payload: dict[str, object]) -> None:
    print(f"success: {payload.get('success')}")
    print(f"stage: {payload.get('stage')}")
    print(f"email: {payload.get('email') or '-'}")
    print(f"account_id: {payload.get('account_id') or '-'}")
    print(f"pool_file: {payload.get('pool_file') or '-'}")
    print(f"error_message: {payload.get('error_message') or '-'}")


def print_json_or_summary(payload: dict[str, object], *, output_json: bool) -> None:
    if output_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_callback_summary(payload)

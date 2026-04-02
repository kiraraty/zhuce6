from __future__ import annotations

import core.setup_wizard as setup_wizard


def test_validate_proxy_prints_actionable_message_when_socks_support_missing() -> None:
    captured: list[str] = []

    class _HttpxMissingSocks:
        def get(self, _url: str, **_kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("Using SOCKS proxy, but the 'socksio' package is not installed")

    original_httpx = setup_wizard.httpx
    setup_wizard.httpx = _HttpxMissingSocks()
    try:
        setup_wizard._validate_proxy(captured.append, "socks5://127.0.0.1:1080")
    finally:
        setup_wizard.httpx = original_httpx

    assert any("缺少 SOCKS 依赖" in line for line in captured)
    assert any("uv sync" in line for line in captured)

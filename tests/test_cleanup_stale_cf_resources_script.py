from __future__ import annotations

import json

from scripts import cleanup_stale_cf_resources


def test_script_cleanup_removes_stale_rules_and_dns(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                'export ZHUCE6_CFMAIL_CF_AUTH_EMAIL="cf@example.com"',
                'export ZHUCE6_CFMAIL_CF_AUTH_KEY="global-key"',
                'export ZHUCE6_CFMAIL_CF_ZONE_ID="zone-1"',
                'export ZHUCE6_CFMAIL_ZONE_NAME="example.test"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "cfmail_accounts.json"
    config_path.write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "name": "active",
                        "worker_domain": "email-api.demo",
                        "email_domain": "auto-live.example.test",
                        "admin_password": "pw",
                        "enabled": True,
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    deleted: list[tuple[str, str]] = []

    class FakeResponse:
        def __init__(self, status_code: int, payload: dict[str, object]):
            self.status_code = status_code
            self._payload = payload
            self.content = b"{}"

        def json(self) -> dict[str, object]:
            return self._payload

    def fake_request(method, url, **kwargs):  # type: ignore[no-untyped-def]
        if method == "GET" and url.endswith("/email/routing/rules?page=1&per_page=100"):
            return FakeResponse(
                200,
                {
                    "success": True,
                    "result": [
                        {
                            "id": "rule-old",
                            "name": "auto-old",
                            "matchers": [{"field": "to", "value": "*@auto-old.example.test"}],
                        },
                        {
                            "id": "rule-live",
                            "name": "auto-live",
                            "matchers": [{"field": "to", "value": "*@auto-live.example.test"}],
                        },
                        {
                            "id": "rule-nova",
                            "name": "nova keep",
                            "matchers": [{"field": "to", "value": "*@nova.example.test"}],
                        },
                    ],
                    "result_info": {"page": 1, "total_pages": 1},
                },
            )
        if method == "GET" and url.endswith("/dns_records?page=1&per_page=100"):
            return FakeResponse(
                200,
                {
                    "success": True,
                    "result": [
                        {"id": "dns-old-mx", "type": "MX", "name": "auto-old.example.test"},
                        {"id": "dns-old-txt", "type": "TXT", "name": "auto-old.example.test"},
                        {"id": "dns-live", "type": "MX", "name": "auto-live.example.test"},
                        {"id": "dns-nova", "type": "TXT", "name": "nova.example.test"},
                        {"id": "dns-ignore", "type": "A", "name": "auto-old.example.test"},
                    ],
                    "result_info": {"page": 1, "total_pages": 1},
                },
            )
        if method == "DELETE":
            deleted.append((method, url))
            return FakeResponse(200, {"success": True, "result": {}})
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(cleanup_stale_cf_resources.cffi_requests, "request", fake_request)

    result = cleanup_stale_cf_resources.run_cleanup(env_file=env_path, config_path=config_path)

    assert result["active_domain"] == "auto-live.example.test"
    assert result["removed_routing_rules"] == ["rule-old"]
    assert sorted(result["removed_dns_records"]) == ["dns-old-mx", "dns-old-txt"]
    assert deleted == [
        ("DELETE", "https://api.cloudflare.com/client/v4/zones/zone-1/email/routing/rules/rule-old"),
        ("DELETE", "https://api.cloudflare.com/client/v4/zones/zone-1/dns_records/dns-old-mx"),
        ("DELETE", "https://api.cloudflare.com/client/v4/zones/zone-1/dns_records/dns-old-txt"),
    ]

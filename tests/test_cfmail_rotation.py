import json

from curl_cffi import requests as cffi_requests

from core.cfmail_domain_rotation import DomainHealthTracker, classify_domain_attempt
from core.cfmail_provisioner import CfmailProvisioner, ProvisioningSettings


def test_classify_domain_attempt_recognizes_blacklist_codes() -> None:
    attempt = classify_domain_attempt(
        {
            "success": False,
            "stage": "create_account",
            "error_message": "create account failed",
            "metadata": {
                "email_domain": "nova.example.test",
                "create_account_error_code": "registration_disallowed",
                "create_account_error_message": "blocked",
            },
        },
        proxy_key="sg-node",
    )

    assert attempt is not None
    assert attempt.domain == "nova.example.test"
    assert attempt.blacklist_code == "registration_disallowed"
    assert attempt.proxy_key == "sg-node"
    assert attempt.backend_failure is False


def test_domain_health_tracker_requires_threshold_before_rotation() -> None:
    tracker = DomainHealthTracker(window_size=3, blacklist_threshold=3, rotation_cooldown_seconds=1)
    payload = {
        "success": False,
        "stage": "create_account",
        "error_message": "create account failed",
        "metadata": {
            "email_domain": "nova.example.test",
            "create_account_error_code": "unsupported_email",
            "create_account_error_message": "unsupported",
        },
    }

    for idx in range(2):
        attempt = classify_domain_attempt(payload, proxy_key=f"proxy-{idx}")
        assert attempt is not None
        decision = tracker.record(attempt)
        assert decision.should_rotate is False

    final_attempt = classify_domain_attempt(payload, proxy_key="proxy-3")
    assert final_attempt is not None
    decision = tracker.record(final_attempt)
    assert decision.should_rotate is True
    assert decision.domain == "nova.example.test"


def test_domain_health_tracker_allows_small_number_of_successes_before_rotation() -> None:
    tracker = DomainHealthTracker(
        window_size=4,
        blacklist_threshold=3,
        rotation_cooldown_seconds=1,
        max_successes_in_window=1,
    )
    blacklist_payload = {
        "success": False,
        "stage": "create_account",
        "error_message": "create account failed",
        "metadata": {
            "email_domain": "nova.example.test",
            "create_account_error_code": "registration_disallowed",
        },
    }
    success_payload = {
        "success": True,
        "stage": "completed",
        "email": "ok@nova.example.test",
        "metadata": {"email_domain": "nova.example.test"},
    }

    tracker.record(classify_domain_attempt(blacklist_payload, proxy_key="p1"))  # type: ignore[arg-type]
    tracker.record(classify_domain_attempt(blacklist_payload, proxy_key="p2"))  # type: ignore[arg-type]
    tracker.record(classify_domain_attempt(success_payload, proxy_key="p3"))  # type: ignore[arg-type]
    decision = tracker.record(classify_domain_attempt(blacklist_payload, proxy_key="p4"))  # type: ignore[arg-type]

    assert decision.should_rotate is True


def test_cfmail_provisioner_switch_active_domain_updates_config(tmp_path) -> None:
    config_path = tmp_path / "cfmail.json"
    config_path.write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "name": "old-active",
                        "worker_domain": "email-api.example.test",
                        "email_domain": "nova.example.test",
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
    provisioner = CfmailProvisioner(
        config_path=config_path,
        settings=ProvisioningSettings(
            auth_email="demo@example.com",
            auth_key="demo-key",
            account_id="acct",
            zone_id="zone",
            worker_name="worker",
            zone_name="example.test",
        ),
    )

    removed_domains = provisioner.switch_active_domain(
        old_domain="nova.example.test",
        new_domain="auto0322.example.test",
        worker_domain="email-api.example.test",
        admin_password="pw",
    )

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    accounts = payload["accounts"]
    assert removed_domains == []
    assert [item["email_domain"] for item in accounts] == [
        "nova.example.test",
        "auto0322.example.test",
    ]
    assert accounts[0]["enabled"] is False
    assert accounts[1]["enabled"] is True


def test_cfmail_provisioner_normalize_accounts_keeps_active_and_previous_auto_domain(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "cfmail.json"
    config_path.write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "name": "cfmail-auto-old1",
                        "worker_domain": "email-api.example.test",
                        "email_domain": "auto-old1.example.test",
                        "admin_password": "pw",
                        "enabled": True,
                    },
                    {
                        "name": "manual-disabled",
                        "worker_domain": "email-api.example.test",
                        "email_domain": "manual.example.test",
                        "admin_password": "pw",
                        "enabled": False,
                    },
                    {
                        "name": "cfmail-auto-live",
                        "worker_domain": "email-api.example.test",
                        "email_domain": "auto-live.example.test",
                        "admin_password": "pw",
                        "enabled": True,
                    },
                ]
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    provisioner = CfmailProvisioner(
        config_path=config_path,
        settings=ProvisioningSettings(
            auth_email="demo@example.com",
            auth_key="demo-key",
            account_id="acct",
            zone_id="zone",
            worker_name="worker",
            zone_name="example.test",
        ),
    )
    deleted_domains: list[str] = []
    monkeypatch.setattr(provisioner, "_delete_domain_artifacts", lambda domain: deleted_domains.append(domain))

    result = provisioner.normalize_accounts_to_single_active_domain()

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    accounts = payload["accounts"]
    assert result == {
        "active_domain": "auto-live.example.test",
        "removed_domains": [],
    }
    assert [item["email_domain"] for item in accounts] == [
        "auto-old1.example.test",
        "manual.example.test",
        "auto-live.example.test",
    ]
    assert deleted_domains == []
    assert accounts[0]["enabled"] is False
    assert accounts[1]["enabled"] is False
    assert accounts[2]["enabled"] is True


def test_cfmail_provisioner_normalize_accounts_keeps_only_latest_previous_auto_domain(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "cfmail.json"
    config_path.write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "name": "cfmail-auto-old1",
                        "worker_domain": "email-api.example.test",
                        "email_domain": "auto-old1.example.test",
                        "admin_password": "pw",
                        "enabled": False,
                    },
                    {
                        "name": "cfmail-auto-old2",
                        "worker_domain": "email-api.example.test",
                        "email_domain": "auto-old2.example.test",
                        "admin_password": "pw",
                        "enabled": False,
                    },
                    {
                        "name": "cfmail-auto-live",
                        "worker_domain": "email-api.example.test",
                        "email_domain": "auto-live.example.test",
                        "admin_password": "pw",
                        "enabled": True,
                    },
                ]
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    provisioner = CfmailProvisioner(
        config_path=config_path,
        settings=ProvisioningSettings(
            auth_email="demo@example.com",
            auth_key="demo-key",
            account_id="acct",
            zone_id="zone",
            worker_name="worker",
            zone_name="example.test",
        ),
    )
    deleted_domains: list[str] = []
    monkeypatch.setattr(provisioner, "_delete_domain_artifacts", lambda domain: deleted_domains.append(domain))

    result = provisioner.normalize_accounts_to_single_active_domain()

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    accounts = payload["accounts"]
    assert result == {
        "active_domain": "auto-live.example.test",
        "removed_domains": ["auto-old1.example.test"],
    }
    assert deleted_domains == ["auto-old1.example.test"]
    assert [item["email_domain"] for item in accounts] == [
        "auto-old2.example.test",
        "auto-live.example.test",
    ]
    assert accounts[0]["enabled"] is False
    assert accounts[1]["enabled"] is True


def test_cfmail_provisioner_smoke_test_retries_non_json_then_succeeds(monkeypatch) -> None:
    provisioner = CfmailProvisioner(
        settings=ProvisioningSettings(
            auth_email="demo@example.com",
            auth_key="demo-key",
            account_id="acct",
            zone_id="zone",
            worker_name="worker",
            zone_name="example.test",
        ),
        proxy_url="http://127.0.0.1:7899",
    )

    class FakeResponse:
        def __init__(self, status_code: int, text: str, payload=None):
            self.status_code = status_code
            self.text = text
            self._payload = payload
            self.content = text.encode("utf-8")

        def json(self):
            if self._payload is None:
                raise ValueError("not json")
            return self._payload

    responses = [
        FakeResponse(200, "<html>pending</html>", None),
        FakeResponse(200, '{"jwt":"x","address":"y"}', {"jwt": "x", "address": "y"}),
        FakeResponse(200, '{"jwt":"x","address":"y"}', {"jwt": "x", "address": "y"}),
        FakeResponse(200, '{"jwt":"x","address":"y"}', {"jwt": "x", "address": "y"}),
    ]

    def fake_post(*args, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(cffi_requests, "post", fake_post)
    monkeypatch.setattr("core.cfmail_provisioner.time.sleep", lambda *_args, **_kwargs: None)

    provisioner.smoke_test("email-api.example.test", "pw", "auto.example.test")


def test_cfmail_provisioner_smoke_test_requires_multiple_successful_creates(monkeypatch) -> None:
    provisioner = CfmailProvisioner(
        settings=ProvisioningSettings(
            auth_email="demo@example.com",
            auth_key="demo-key",
            account_id="acct",
            zone_id="zone",
            worker_name="worker",
            zone_name="example.test",
        ),
    )

    class FakeResponse:
        def __init__(self) -> None:
            self.status_code = 200
            self.text = '{"jwt":"x","address":"y"}'
            self._payload = {"jwt": "x", "address": "y"}
            self.content = self.text.encode("utf-8")

        def json(self):
            return self._payload

    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeResponse()

    monkeypatch.setattr(cffi_requests, "post", fake_post)

    provisioner.smoke_test("email-api.example.test", "pw", "auto.example.test")

    assert len(calls) == 3


def test_cfmail_provisioner_cleanup_stale_domains_removes_old_auto_resources(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / 'cfmail.json'
    config_path.write_text(
        json.dumps({
            'accounts': [
                {'name': 'old-1', 'worker_domain': 'email-api.demo', 'email_domain': 'auto-old1.example.test', 'admin_password': 'pw', 'enabled': False},
                {'name': 'old-2', 'worker_domain': 'email-api.demo', 'email_domain': 'auto-old2.example.test', 'admin_password': 'pw', 'enabled': False},
                {'name': 'keep', 'worker_domain': 'email-api.demo', 'email_domain': 'auto-keep.example.test', 'admin_password': 'pw', 'enabled': False},
                {'name': 'active', 'worker_domain': 'email-api.demo', 'email_domain': 'auto-live.example.test', 'admin_password': 'pw', 'enabled': True},
                {'name': 'base', 'worker_domain': 'email-api.demo', 'email_domain': 'inbox.example.test', 'admin_password': 'pw', 'enabled': False},
            ]
        }, ensure_ascii=False, indent=2) + "\n",
        encoding='utf-8',
    )
    provisioner = CfmailProvisioner(
        config_path=config_path,
        settings=ProvisioningSettings(
            auth_email='demo@example.com', auth_key='demo-key', account_id='acct', zone_id='zone', worker_name='worker', zone_name='example.test'
        ),
    )
    deleted_dns = []
    deleted_rules = []
    monkeypatch.setattr(provisioner, '_list_dns_records', lambda: [
        {'id': 'dns-old1', 'type': 'MX', 'name': 'auto-old1.example.test'},
        {'id': 'dns-old2', 'type': 'TXT', 'name': 'auto-old2.example.test'},
        {'id': 'dns-keep', 'type': 'MX', 'name': 'auto-keep.example.test'},
        {'id': 'dns-live', 'type': 'MX', 'name': 'auto-live.example.test'},
    ])
    monkeypatch.setattr(provisioner, '_list_email_routing_rules', lambda: [
        {'id': 'rule-old1', 'matchers': [{'field': 'to', 'value': '*@auto-old1.example.test'}]},
        {'id': 'rule-old2', 'matchers': [{'field': 'to', 'value': '*@auto-old2.example.test'}]},
        {'id': 'rule-keep', 'matchers': [{'field': 'to', 'value': '*@auto-keep.example.test'}]},
    ])
    monkeypatch.setattr(provisioner, '_delete_dns_record', lambda record_id: deleted_dns.append(record_id))
    monkeypatch.setattr(provisioner, '_delete_email_routing_rule', lambda rule_id: deleted_rules.append(rule_id))

    result = provisioner.cleanup_stale_domains()

    assert sorted(result['removed_domains']) == ['auto-keep.example.test', 'auto-old1.example.test', 'auto-old2.example.test']
    assert sorted(deleted_dns) == ['dns-keep', 'dns-old1', 'dns-old2']
    assert sorted(deleted_rules) == ['rule-keep', 'rule-old1', 'rule-old2']


def test_cfmail_provisioner_cleanup_stale_domains_skips_read_only_artifacts(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "cfmail.json"
    config_path.write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "name": "old-1",
                        "worker_domain": "email-api.demo",
                        "email_domain": "auto-old1.example.test",
                        "admin_password": "pw",
                        "enabled": False,
                    },
                    {
                        "name": "old-2",
                        "worker_domain": "email-api.demo",
                        "email_domain": "auto-old2.example.test",
                        "admin_password": "pw",
                        "enabled": False,
                    },
                    {
                        "name": "active",
                        "worker_domain": "email-api.demo",
                        "email_domain": "auto-live.example.test",
                        "admin_password": "pw",
                        "enabled": True,
                    },
                ]
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    provisioner = CfmailProvisioner(
        config_path=config_path,
        settings=ProvisioningSettings(
            auth_email="demo@example.com",
            auth_key="demo-key",
            account_id="acct",
            zone_id="zone",
            worker_name="worker",
            zone_name="example.test",
        ),
    )
    deleted_dns: list[str] = []
    deleted_rules: list[str] = []
    monkeypatch.setattr(
        provisioner,
        "_list_dns_records",
        lambda: [
            {"id": "dns-old1", "type": "MX", "name": "auto-old1.example.test"},
            {"id": "dns-old2", "type": "TXT", "name": "auto-old2.example.test"},
        ],
    )
    monkeypatch.setattr(
        provisioner,
        "_list_email_routing_rules",
        lambda: [
            {"id": "rule-old1", "matchers": [{"field": "to", "value": "*@auto-old1.example.test"}]},
            {"id": "rule-old2", "matchers": [{"field": "to", "value": "*@auto-old2.example.test"}]},
        ],
    )

    def fake_delete_dns(record_id: str) -> None:
        deleted_dns.append(record_id)
        if record_id == "dns-old1":
            raise RuntimeError('HTTP 400 {"errors":[{"code":1043,"message":"DNS record is read only"}]}')

    def fake_delete_rule(rule_id: str) -> None:
        deleted_rules.append(rule_id)
        if rule_id == "rule-old1":
            raise RuntimeError('HTTP 400 {"errors":[{"code":1043,"message":"routing rule is read only"}]}')

    monkeypatch.setattr(provisioner, "_delete_dns_record", fake_delete_dns)
    monkeypatch.setattr(provisioner, "_delete_email_routing_rule", fake_delete_rule)

    result = provisioner.cleanup_stale_domains()

    assert sorted(result["removed_domains"]) == ["auto-old1.example.test", "auto-old2.example.test"]
    assert result["removed_dns_records"] == ["dns-old2"]
    assert result["removed_routing_rules"] == ["rule-old2"]


def test_cfmail_provisioner_cleanup_stale_cf_resources_discovers_cf_side_orphans(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "cfmail.json"
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
    provisioner = CfmailProvisioner(
        config_path=config_path,
        settings=ProvisioningSettings(
            auth_email="demo@example.com",
            auth_key="demo-key",
            account_id="acct",
            zone_id="zone",
            worker_name="worker",
            zone_name="example.test",
        ),
    )
    deleted_dns: list[str] = []
    deleted_rules: list[str] = []
    monkeypatch.setattr(
        provisioner,
        "_list_dns_records",
        lambda: [
            {"id": "dns-old1-mx", "type": "MX", "name": "auto-old1.example.test"},
            {"id": "dns-old1-txt", "type": "TXT", "name": "auto-old1.example.test"},
            {"id": "dns-old2-mx", "type": "MX", "name": "auto-old2.example.test"},
            {"id": "dns-active", "type": "MX", "name": "auto-live.example.test"},
            {"id": "dns-nova", "type": "MX", "name": "nova.example.test"},
            {"id": "dns-ignore", "type": "A", "name": "auto-old1.example.test"},
            {"id": "dns-outside", "type": "TXT", "name": "auto-old1.other.test"},
        ],
    )
    monkeypatch.setattr(
        provisioner,
        "_list_email_routing_rules",
        lambda: [
            {
                "id": "rule-old1",
                "name": "old1 subdomain catch-all",
                "matchers": [{"field": "to", "value": "*@auto-old1.example.test"}],
            },
            {
                "id": "rule-old2",
                "name": "old2 subdomain catch-all",
                "matchers": [{"field": "to", "value": "*@auto-old2.example.test"}],
            },
            {
                "id": "rule-active",
                "name": "active subdomain catch-all",
                "matchers": [{"field": "to", "value": "*@auto-live.example.test"}],
            },
            {
                "id": "rule-nova",
                "name": "nova keep",
                "matchers": [{"field": "to", "value": "*@nova.example.test"}],
            },
        ],
    )

    def fake_delete_dns(record_id: str) -> None:
        if record_id == "dns-old2-mx":
            raise RuntimeError("dns delete failed")
        deleted_dns.append(record_id)

    def fake_delete_rule(rule_id: str) -> None:
        if rule_id == "rule-old2":
            raise RuntimeError("rule delete failed")
        deleted_rules.append(rule_id)

    monkeypatch.setattr(provisioner, "_delete_dns_record", fake_delete_dns)
    monkeypatch.setattr(provisioner, "_delete_email_routing_rule", fake_delete_rule)

    result = provisioner.cleanup_stale_cf_resources()

    assert sorted(result["removed_dns_records"]) == ["dns-old1-mx", "dns-old1-txt"]
    assert result["removed_routing_rules"] == ["rule-old1"]
    assert len(result["errors"]) == 2
    assert any("dns-old2-mx" in error for error in result["errors"])
    assert any("rule-old2" in error for error in result["errors"])
    assert deleted_dns == ["dns-old1-mx", "dns-old1-txt"]
    assert deleted_rules == ["rule-old1"]


def test_cfmail_provisioner_delete_domain_artifacts_skips_read_only_records(monkeypatch, tmp_path) -> None:
    provisioner = CfmailProvisioner(
        config_path=tmp_path / "cfmail.json",
        settings=ProvisioningSettings(
            auth_email="demo@example.com",
            auth_key="demo-key",
            account_id="acct",
            zone_id="zone",
            worker_name="worker",
            zone_name="example.test",
        ),
    )
    deleted_dns: list[str] = []
    deleted_rules: list[str] = []
    monkeypatch.setattr(
        provisioner,
        "_list_dns_records",
        lambda: [
            {"id": "dns-1", "name": "auto-old.example.test"},
            {"id": "dns-2", "name": "auto-old.example.test"},
        ],
    )
    monkeypatch.setattr(
        provisioner,
        "_list_email_routing_rules",
        lambda: [
            {"id": "rule-1", "matchers": [{"field": "to", "value": "*@auto-old.example.test"}]},
            {"id": "rule-2", "matchers": [{"field": "to", "value": "*@auto-old.example.test"}]},
        ],
    )

    def fake_delete_dns(record_id: str) -> None:
        deleted_dns.append(record_id)
        if record_id == "dns-1":
            raise RuntimeError("read only")

    def fake_delete_rule(rule_id: str) -> None:
        deleted_rules.append(rule_id)
        if rule_id == "rule-1":
            raise RuntimeError("read only")

    monkeypatch.setattr(provisioner, "_delete_dns_record", fake_delete_dns)
    monkeypatch.setattr(provisioner, "_delete_email_routing_rule", fake_delete_rule)

    provisioner._delete_domain_artifacts("auto-old.example.test")

    assert deleted_dns == ["dns-1", "dns-2"]
    assert deleted_rules == ["rule-1", "rule-2"]


def test_cfmail_provisioner_rotate_retries_after_record_quota_cleanup(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / 'cfmail.json'
    config_path.write_text(json.dumps({'accounts':[{'name':'active','worker_domain':'email-api.demo','email_domain':'auto-live.example.test','admin_password':'pw','enabled':True}]}) + "\n", encoding='utf-8')
    provisioner = CfmailProvisioner(
        config_path=config_path,
        settings=ProvisioningSettings(
            auth_email='demo@example.com', auth_key='demo-key', account_id='acct', zone_id='zone', worker_name='worker', zone_name='example.test'
        ),
    )
    labels = iter(['auto-old', 'auto-new'])
    monkeypatch.setattr(provisioner, '_make_new_label', lambda: next(labels))
    create_calls = []
    cleanup_calls = []
    switch_calls = []

    monkeypatch.setattr(provisioner, '_create_email_routing_rule', lambda domain, label: create_calls.append(('rule', domain, label)))
    def fake_create_dns(domain):
        create_calls.append(('dns', domain))
        if domain == 'auto-old.example.test':
            raise RuntimeError('POST dns failed: HTTP 400 {"errors":[{"code":81045,"message":"Record quota exceeded."}]}')
    monkeypatch.setattr(provisioner, '_create_dns_records', fake_create_dns)
    monkeypatch.setattr(provisioner, '_update_worker_domains', lambda domain, old_domain=None: create_calls.append(('worker', domain, old_domain)))
    monkeypatch.setattr(provisioner, 'smoke_test', lambda *args: create_calls.append(('smoke', args[2])))
    monkeypatch.setattr(provisioner, 'switch_active_domain', lambda **kwargs: switch_calls.append(kwargs) or [])
    monkeypatch.setattr(
        provisioner,
        'cleanup_stale_cf_resources',
        lambda keep_domains=None: cleanup_calls.append(keep_domains) or {'removed_dns_records':['dns-old'], 'removed_routing_rules':['rule-old'], 'errors': []},
    )
    monkeypatch.setattr(provisioner, '_delete_domain_artifacts', lambda domain: create_calls.append(('cleanup_domain', domain)))

    result = provisioner.rotate_active_domain()

    assert result.success is True
    assert result.new_domain == 'auto-new.example.test'
    assert cleanup_calls == [None, {'auto-live.example.test'}]
    assert ('cleanup_domain', 'auto-old.example.test') in create_calls
    assert switch_calls[0]['new_domain'] == 'auto-new.example.test'


def test_cfmail_provisioner_rotate_keeps_success_when_cleanup_after_switch_fails(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "cfmail.json"
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
            }
        )
        + "\n",
        encoding="utf-8",
    )
    provisioner = CfmailProvisioner(
        config_path=config_path,
        settings=ProvisioningSettings(
            auth_email="demo@example.com",
            auth_key="demo-key",
            account_id="acct",
            zone_id="zone",
            worker_name="worker",
            zone_name="example.test",
        ),
    )
    switch_calls: list[dict[str, str]] = []

    monkeypatch.setattr(provisioner, "_make_new_label", lambda: "auto-next")
    monkeypatch.setattr(provisioner, "_create_email_routing_rule", lambda domain, label: None)
    monkeypatch.setattr(provisioner, "_create_dns_records", lambda domain: None)
    monkeypatch.setattr(provisioner, "_update_worker_domains", lambda domain, old_domain=None: None)
    monkeypatch.setattr(provisioner, "smoke_test", lambda *args: None)
    monkeypatch.setattr(
        provisioner,
        "switch_active_domain",
        lambda **kwargs: switch_calls.append(kwargs) or [],
    )

    def fake_cleanup(keep_domains=None):  # type: ignore[no-untyped-def]
        raise RuntimeError("HTTP 400 read only")

    monkeypatch.setattr(provisioner, "cleanup_stale_cf_resources", fake_cleanup)
    monkeypatch.setattr(provisioner, "_delete_domain_artifacts", lambda domain: (_ for _ in ()).throw(AssertionError("should not rollback")))

    result = provisioner.rotate_active_domain()

    assert result.success is True
    assert result.new_domain == "auto-next.example.test"
    assert switch_calls[0]["new_domain"] == "auto-next.example.test"


def test_cfmail_provisioner_patch_worker_settings_uses_multipart_request_and_total_timeout(tmp_path, monkeypatch) -> None:
    provisioner = CfmailProvisioner(
        config_path=tmp_path / "cfmail.json",
        settings=ProvisioningSettings(
            auth_email="demo@example.com",
            auth_key="demo-key",
            account_id="acct",
            zone_id="zone",
            worker_name="worker",
            zone_name="example.test",
        ),
        proxy_url="http://127.0.0.1:7890",
    )
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200
        content = b'{"success":true}'

        def json(self):  # type: ignore[no-untyped-def]
            return {"success": True}

    def fake_patch(url, **kwargs):  # type: ignore[no-untyped-def]
        captured["url"] = url
        captured["kwargs"] = kwargs
        return FakeResponse()

    monkeypatch.setattr(cffi_requests, "patch", fake_patch)

    provisioner._patch_worker_settings([{"name": "DOMAINS", "type": "json", "json": ["auto-live.example.test"]}])

    kwargs = captured["kwargs"]
    assert captured["url"] == "https://api.cloudflare.com/client/v4/accounts/acct/workers/scripts/worker/settings"
    assert kwargs["timeout"] == 30
    assert kwargs["proxies"] == {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
    assert kwargs["headers"]["Content-Type"].startswith("multipart/form-data; boundary=")
    assert b'"bindings":[{"name":"DOMAINS","type":"json","json":["auto-live.example.test"]}]' in kwargs["data"]


def test_cfmail_provisioner_update_worker_domains_keeps_new_and_previous_domain_only(tmp_path, monkeypatch) -> None:
    provisioner = CfmailProvisioner(
        config_path=tmp_path / "cfmail.json",
        settings=ProvisioningSettings(
            auth_email="demo@example.com",
            auth_key="demo-key",
            account_id="acct",
            zone_id="zone",
            worker_name="worker",
            zone_name="example.test",
        ),
    )
    patched_bindings: list[dict[str, object]] = []
    monkeypatch.setattr(
        provisioner,
        "_get_worker_settings",
        lambda: {
            "bindings": [
                {"name": "DOMAINS", "type": "json", "json": ["auto-old1.example.test", "auto-old2.example.test"]},
                {"name": "DEFAULT_DOMAINS", "type": "json", "json": ["auto-old1.example.test", "auto-old2.example.test"]},
                {"name": "UNRELATED", "type": "plain_text", "text": "keep"},
            ]
        },
    )
    monkeypatch.setattr(provisioner, "_patch_worker_settings", lambda bindings: patched_bindings.extend(bindings))

    provisioner._update_worker_domains("auto-live.example.test", old_domain="auto-old2.example.test")

    assert patched_bindings == [
        {"name": "DOMAINS", "type": "json", "json": ["auto-live.example.test", "auto-old2.example.test"]},
        {"name": "DEFAULT_DOMAINS", "type": "json", "json": ["auto-live.example.test", "auto-old2.example.test"]},
        {"name": "UNRELATED", "type": "plain_text", "text": "keep"},
    ]


def test_cfmail_provisioner_rotate_keeps_previous_domain_artifacts_for_dual_domain_grace(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "cfmail.json"
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
            }
        )
        + "\n",
        encoding="utf-8",
    )
    provisioner = CfmailProvisioner(
        config_path=config_path,
        settings=ProvisioningSettings(
            auth_email="demo@example.com",
            auth_key="demo-key",
            account_id="acct",
            zone_id="zone",
            worker_name="worker",
            zone_name="example.test",
        ),
    )
    patched_worker_domains: list[tuple[str, str | None]] = []
    deleted_domains: list[str] = []
    cleanup_keep_domains: list[set[str]] = []

    monkeypatch.setattr(provisioner, "_make_new_label", lambda: "auto-next")
    monkeypatch.setattr(provisioner, "_create_email_routing_rule", lambda domain, label: None)
    monkeypatch.setattr(provisioner, "_create_dns_records", lambda domain: None)
    monkeypatch.setattr(
        provisioner,
        "_update_worker_domains",
        lambda domain, old_domain=None: patched_worker_domains.append((domain, old_domain)),
    )
    monkeypatch.setattr(provisioner, "smoke_test", lambda *args: None)
    monkeypatch.setattr(
        provisioner,
        "_delete_domain_artifacts",
        lambda domain: deleted_domains.append(domain),
    )
    monkeypatch.setattr(
        provisioner,
        "cleanup_stale_cf_resources",
        lambda keep_domains=None: cleanup_keep_domains.append(set(keep_domains or [])) or {
            "removed_domains": [],
            "removed_dns_records": [],
            "removed_routing_rules": [],
            "errors": [],
        },
    )

    result = provisioner.rotate_active_domain()

    assert result.success is True
    assert patched_worker_domains == [("auto-next.example.test", "auto-live.example.test")]
    assert deleted_domains == []
    assert cleanup_keep_domains == [{"auto-live.example.test"}]


def test_domain_health_tracker_default_thresholds_are_aggressive(monkeypatch) -> None:
    monkeypatch.delenv("ZHUCE6_CFMAIL_ROTATION_WINDOW", raising=False)
    monkeypatch.delenv("ZHUCE6_CFMAIL_ROTATION_BLACKLIST_THRESHOLD", raising=False)
    monkeypatch.delenv("ZHUCE6_CFMAIL_ROTATION_MAX_SUCCESSES", raising=False)

    tracker = DomainHealthTracker()

    assert tracker.window_size == 10
    assert tracker.blacklist_threshold == 6
    assert tracker.max_successes_in_window == 2

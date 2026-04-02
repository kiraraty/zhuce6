from pathlib import Path

from core.proxy_pool import (
    ManagedProxy,
    ProxyLease,
    ProxyNode,
    ProxyPool,
    parse_clash_ss_nodes,
    parse_direct_proxy_urls,
)


def test_parse_clash_ss_nodes_respects_excludes_and_preferred_patterns(tmp_path: Path) -> None:
    config = tmp_path / "clash.yaml"
    config.write_text(
        """
proxies:
  - { name: "新加坡优化-2", type: ss, server: "sg.bad", port: 1234, cipher: "aes-256-gcm", password: "pw" }
  - { name: "新加坡原生解锁-1", type: ss, server: "sg.good", port: 2345, cipher: "aes-256-gcm", password: "pw" }
  - { name: "★三网-日本备用", type: ss, server: "jp.good", port: 3456, cipher: "aes-256-gcm", password: "pw" }
  - { name: "台湾-三网备用", type: ss, server: "tw.good", port: 4567, cipher: "aes-256-gcm", password: "pw" }
""".strip(),
        encoding="utf-8",
    )

    nodes = parse_clash_ss_nodes(
        config,
        ("sg", "jp", "tw"),
        exclude_names=("新加坡优化-2",),
        preferred_name_patterns=("新加坡原生解锁", "★三网-日本备用"),
    )

    assert [node.name for node in nodes] == [
        "新加坡原生解锁-1",
        "★三网-日本备用",
        "台湾-三网备用",
    ]


def test_proxy_pool_cooldowns_node_after_repeated_device_id_failures() -> None:
    node = ProxyNode(name="sg-node", server="sg.good", port=1234, cipher="aes-256-gcm", password="pw", region="sg")
    pool = ProxyPool(nodes=[node], size=1, executable="/bin/true")
    managed = ManagedProxy(node=node, local_port=17891)
    pool._managed = [managed]
    pool._started = True

    lease = ProxyLease(name="sg-node", local_port=17891, proxy_url="socks5://127.0.0.1:17891")
    pool.release(lease, success=False, stage="device_id")
    pool.release(lease, success=False, stage="device_id")

    snapshot = pool.snapshot()
    assert snapshot[0]["device_id_failures"] == 2
    assert snapshot[0]["device_id_consecutive_failures"] == 2
    assert snapshot[0]["cooldown_reason"] == "device_id_failures"
    assert pool._available() == []


def test_proxy_pool_prefers_nodes_with_better_device_id_history() -> None:
    bad_node = ProxyNode(name="tw-bad", server="tw.bad", port=1234, cipher="aes-256-gcm", password="pw", region="tw")
    good_node = ProxyNode(name="sg-good", server="sg.good", port=2345, cipher="aes-256-gcm", password="pw", region="sg")
    pool = ProxyPool(nodes=[bad_node, good_node], size=2, executable="/bin/true")
    pool._managed = [
        ManagedProxy(node=bad_node, local_port=17891, device_id_failures=2, device_id_consecutive_failures=1),
        ManagedProxy(node=good_node, local_port=17892, device_id_successes=3),
    ]
    pool._started = True

    available = pool._available()

    assert [item.node.name for item in available] == ["sg-good", "tw-bad"]


def test_direct_proxy_parse_and_pool_start() -> None:
    direct_nodes = parse_direct_proxy_urls("socks5://1.2.3.4:1080;http://5.6.7.8:8080")

    pool = ProxyPool(nodes=[], direct_nodes=direct_nodes, size=2, executable=None)
    pool.start()

    lease = pool.acquire()
    assert lease.proxy_url in {"socks5://1.2.3.4:1080", "http://5.6.7.8:8080"}
    assert lease.local_port >= 17891

    pool.release(lease, success=True)
    snapshot = pool.snapshot()
    assert len(snapshot) == 2
    assert {item["proxy_url"] for item in snapshot} == {"socks5://1.2.3.4:1080", "http://5.6.7.8:8080"}
    assert snapshot[0]["disabled"] is False


def test_direct_proxy_mixed_with_ss(monkeypatch) -> None:
    class DummyProcess:
        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 0

    monkeypatch.setattr("core.proxy_pool.subprocess.Popen", lambda *args, **kwargs: DummyProcess())
    ss_node = ProxyNode(name="sg-node", server="sg.good", port=1234, cipher="aes-256-gcm", password="pw", region="sg")
    direct_nodes = parse_direct_proxy_urls("http://5.6.7.8:8080")

    pool = ProxyPool(nodes=[ss_node], direct_nodes=direct_nodes, size=2, executable="/usr/bin/sslocal")
    pool.start()

    first = pool.acquire()
    second = pool.acquire()
    urls = {first.proxy_url, second.proxy_url}
    assert "http://5.6.7.8:8080" in urls
    assert any(url.startswith("socks5://127.0.0.1:") for url in urls)

    pool.release(first, success=True)
    pool.release(second, success=True)


def test_direct_proxy_invalid_url_skipped(capsys) -> None:
    nodes = parse_direct_proxy_urls("ftp://1.2.3.4:21;not-a-url;https://good.example:8443")

    captured = capsys.readouterr()
    assert [node.proxy_url for node in nodes] == ["https://good.example:8443"]
    assert "invalid direct proxy url" in captured.err

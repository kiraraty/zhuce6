"""SS-only proxy pool for zhuce6 registration workers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
from typing import Any
from urllib.parse import urlparse

import yaml


SKIP_NAME_MARKERS = (
    "流量",
    "续费",
    "到期",
    "订阅",
    "官网",
    "客服",
    "购买",
    "套餐",
    "说明",
)

REGION_ALIASES: dict[str, tuple[str, ...]] = {
    "sg": ("sg", "singapore", "新加坡"),
    "hk": ("hk", "hong kong", "香港"),
    "jp": ("jp", "japan", "日本"),
    "us": ("us", "usa", "united states", "美国"),
    "tw": ("tw", "taiwan", "台湾"),
}

DEVICE_ID_FAIL_COOLDOWN_SECONDS = 600
# After each use (success or failure), a node enters a short cooldown to
# distribute traffic across IPs and reduce risk-control pressure.
PER_USE_COOLDOWN_SECONDS = 120


@dataclass(frozen=True)
class ProxyNode:
    name: str
    server: str
    port: int
    cipher: str
    password: str
    region: str


@dataclass(frozen=True)
class DirectProxyNode:
    name: str
    proxy_url: str
    region: str = "direct"


@dataclass(frozen=True)
class ProxyLease:
    name: str
    local_port: int
    proxy_url: str


@dataclass
class ManagedProxy:
    node: ProxyNode | DirectProxyNode
    local_port: int
    process: subprocess.Popen[Any] | None = None
    in_use: bool = False
    disabled: bool = False
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    device_id_successes: int = 0
    device_id_failures: int = 0
    device_id_consecutive_failures: int = 0
    cooldown_until: float | None = None
    cooldown_reason: str = ""
    last_error: str = ""
    last_checked_at: float | None = None
    last_released_at: float = 0.0

    @property
    def proxy_url(self) -> str:
        if isinstance(self.node, DirectProxyNode):
            return self.node.proxy_url
        return f"socks5://127.0.0.1:{self.local_port}"


def _normalize_region_name(raw: str) -> str:
    text = raw.strip().lower()
    for region, aliases in REGION_ALIASES.items():
        if any(alias in text for alias in aliases):
            return region
    return "other"


def _should_skip_name(name: str) -> bool:
    lowered = name.strip().lower()
    return any(marker.lower() in lowered for marker in SKIP_NAME_MARKERS)


def _matches_any_name(name: str, patterns: tuple[str, ...]) -> bool:
    lowered = name.strip().lower()
    return any(pattern.strip().lower() in lowered for pattern in patterns if pattern.strip())


def parse_clash_ss_nodes(
    config_path: str | Path,
    preferred_regions: tuple[str, ...] = (),
    *,
    exclude_names: tuple[str, ...] = (),
    preferred_name_patterns: tuple[str, ...] = (),
) -> list[ProxyNode]:
    payload = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    proxies = payload.get("proxies") if isinstance(payload, dict) else []
    items = proxies if isinstance(proxies, list) else []
    nodes: list[ProxyNode] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "").strip().lower() != "ss":
            continue
        name = str(item.get("name") or "").strip()
        if not name or _should_skip_name(name):
            continue
        if _matches_any_name(name, exclude_names):
            continue
        server = str(item.get("server") or "").strip()
        cipher = str(item.get("cipher") or "").strip()
        password = str(item.get("password") or "").strip()
        try:
            port = int(item.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        if not server or not cipher or not password or port <= 0:
            continue
        nodes.append(
            ProxyNode(
                name=name,
                server=server,
                port=port,
                cipher=cipher,
                password=password,
                region=_normalize_region_name(name),
            )
        )

    region_order = {region: index for index, region in enumerate(preferred_regions)}
    return sorted(
        nodes,
        key=lambda node: (
            0 if _matches_any_name(node.name, preferred_name_patterns) else 1,
            region_order.get(node.region, 999),
            node.name.lower(),
        ),
    )


def parse_direct_proxy_urls(raw: str) -> list[DirectProxyNode]:
    nodes: list[DirectProxyNode] = []
    seen_names: set[str] = set()
    for index, chunk in enumerate(str(raw or "").split(";"), start=1):
        proxy_url = chunk.strip()
        if not proxy_url:
            continue
        parsed = urlparse(proxy_url)
        if parsed.scheme not in {"http", "https", "socks4", "socks5"} or not parsed.hostname or parsed.port is None:
            print(f"[proxy_pool] invalid direct proxy url skipped: {proxy_url}", flush=True, file=__import__("sys").stderr)
            continue
        base_name = f"direct-{parsed.hostname}:{parsed.port}"
        name = base_name
        if name in seen_names:
            name = f"{base_name}-{index}"
        seen_names.add(name)
        nodes.append(DirectProxyNode(name=name, proxy_url=proxy_url))
    return nodes


def _detect_ss_local_binary() -> str | None:
    return shutil.which("sslocal") or shutil.which("ss-local")


def _find_open_port(start: int = 17891) -> int:
    port = start
    while port < 65535:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                port += 1
    raise RuntimeError("no free local port available for proxy pool")


class ProxyPool:
    def __init__(
        self,
        *,
        nodes: list[ProxyNode],
        direct_nodes: list[DirectProxyNode] | None = None,
        size: int = 6,
        preferred_regions: tuple[str, ...] = (),
        executable: str | None = None,
    ) -> None:
        self.nodes = list(nodes)
        self.direct_nodes = list(direct_nodes or [])
        self._all_nodes: list[ProxyNode | DirectProxyNode] = [*self.nodes, *self.direct_nodes]
        self.size = max(1, size)
        self.preferred_regions = preferred_regions
        self.executable = executable or _detect_ss_local_binary()
        self._managed: list[ManagedProxy] = []
        self._used_node_names: set[str] = set()
        self._next_local_port = 17891
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._started = False

    @classmethod
    def from_settings(cls, settings: Any) -> "ProxyPool" | None:
        config_path = getattr(settings, "proxy_pool_config", None)
        direct_urls = str(getattr(settings, "proxy_pool_direct_urls", "") or "").strip()
        if not config_path and not direct_urls:
            return None
        nodes: list[ProxyNode] = []
        if config_path:
            nodes = parse_clash_ss_nodes(
                config_path,
                getattr(settings, "proxy_pool_regions", ()),
                exclude_names=tuple(getattr(settings, "proxy_pool_exclude_names", ())),
                preferred_name_patterns=tuple(getattr(settings, "proxy_pool_preferred_patterns", ())),
            )
        direct_nodes = parse_direct_proxy_urls(direct_urls)
        if not nodes and not direct_nodes:
            return None
        return cls(
            nodes=nodes,
            direct_nodes=direct_nodes,
            size=int(getattr(settings, "proxy_pool_size", 6)),
            preferred_regions=tuple(getattr(settings, "proxy_pool_regions", ())),
        )

    def _command(self, node: ProxyNode, local_port: int) -> list[str]:
        if not self.executable:
            raise RuntimeError("ss-local executable not found")
        is_rust = self.executable.endswith("sslocal")
        if is_rust:
            return [
                self.executable,
                "-s", f"{node.server}:{node.port}",
                "-b", f"127.0.0.1:{local_port}",
                "-k", node.password,
                "-m", node.cipher,
                "-U",
            ]
        return [
            self.executable,
            "-s", node.server,
            "-p", str(node.port),
            "-l", str(local_port),
            "-k", node.password,
            "-m", node.cipher,
            "-b", "127.0.0.1",
            "-u",
        ]

    def start(self) -> None:
        with self._cond:
            if self._started:
                return
            self._managed = []
            self._used_node_names = set()
            self._next_local_port = 17891
            target = min(self.size, len(self._all_nodes))
            while len(self._managed) < target:
                if not self._spawn_next_node():
                    break
            if not self._managed and self.nodes and not self.executable:
                raise RuntimeError("ss-local executable not found")
            self._started = True

    def _ensure_started(self) -> None:
        if not self._started:
            self.start()

    def _available(self) -> list[ManagedProxy]:
        import random

        candidates: list[ManagedProxy] = []
        now = time.time()
        for item in self._managed:
            process = item.process
            if process is not None and process.poll() is not None:
                item.disabled = True
                item.last_error = f"process exited with code {process.poll()}"
            if item.cooldown_until is not None and item.cooldown_until <= now:
                item.cooldown_until = None
                item.cooldown_reason = ""
            if item.disabled or item.in_use:
                continue
            if item.cooldown_until is not None and item.cooldown_until > now:
                continue
            # Per-use cooldown: skip nodes that were used very recently.
            if item.last_released_at and (now - item.last_released_at) < PER_USE_COOLDOWN_SECONDS:
                continue
            candidates.append(item)

        # If all candidates are in per-use cooldown, fall back to the full
        # list (minus disabled/in_use/device_id cooldown) so we don't block.
        if not candidates:
            for item in self._managed:
                if item.disabled or item.in_use:
                    continue
                if item.cooldown_until is not None and item.cooldown_until > now:
                    continue
                candidates.append(item)

        # --- Region-aware rotation ---
        # Group candidates by region, pick the region that was used least
        # recently, then pick the best node within that region.
        if candidates:
            region_last_used: dict[str, float] = {}
            region_groups: dict[str, list[ManagedProxy]] = {}
            for item in candidates:
                r = item.node.region
                region_groups.setdefault(r, []).append(item)
                t = item.last_released_at or 0.0
                if r not in region_last_used or t < region_last_used[r]:
                    region_last_used[r] = t

            # Sort regions by least-recently-used first, with random jitter
            # to break ties.
            sorted_regions = sorted(
                region_groups.keys(),
                key=lambda r: (region_last_used.get(r, 0.0), random.random()),
            )

            result: list[ManagedProxy] = []
            for region in sorted_regions:
                group = region_groups[region]
                # Within a region, sort by quality then shuffle equal-quality
                # nodes via random tiebreaker.
                group.sort(
                    key=lambda item: (
                        item.device_id_consecutive_failures > 0,
                        -(item.device_id_successes - item.device_id_failures),
                        item.device_id_failures,
                        item.failures >= 3,
                        -(item.successes - item.failures),
                        item.failures,
                        random.random(),
                    ),
                )
                result.extend(group)
            return result

        return candidates

    def _spawn_next_node(self) -> bool:
        for node in self._all_nodes:
            if node.name in self._used_node_names:
                continue
            if isinstance(node, DirectProxyNode):
                local_port = self._next_local_port
                self._next_local_port += 1
                process = None
            else:
                if not self.executable:
                    continue
                local_port = _find_open_port(self._next_local_port)
                self._next_local_port = local_port + 1
                process = subprocess.Popen(  # noqa: S603
                    self._command(node, local_port),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            self._managed.append(
                ManagedProxy(
                    node=node,
                    local_port=local_port,
                    process=process,
                    last_checked_at=time.time(),
                )
            )
            self._used_node_names.add(node.name)
            return True
        return False

    def acquire(self, timeout: float = 5.0) -> ProxyLease:
        deadline = time.time() + timeout
        with self._cond:
            self._ensure_started()
            while True:
                available = self._available()
                if available:
                    item = available[0]
                    item.in_use = True
                    item.last_checked_at = time.time()
                    return ProxyLease(
                        name=item.node.name,
                        local_port=item.local_port,
                        proxy_url=item.proxy_url,
                    )
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise RuntimeError("no proxy available in pool")
                self._cond.wait(timeout=min(0.2, remaining))

    def release(self, lease: ProxyLease, *, success: bool | None, stage: str | None = None) -> None:
        with self._cond:
            for item in self._managed:
                if item.node.name != lease.name or item.local_port != lease.local_port:
                    continue
                item.in_use = False
                item.last_checked_at = time.time()
                item.last_released_at = time.time()
                stage_key = str(stage or "").strip().lower()
                if success is True:
                    item.successes += 1
                    item.consecutive_failures = 0
                    item.device_id_successes += 1
                    item.device_id_consecutive_failures = 0
                    item.cooldown_until = None
                    item.cooldown_reason = ""
                elif success is False:
                    item.failures += 1
                    item.consecutive_failures += 1
                    if stage_key == "device_id":
                        item.device_id_failures += 1
                        item.device_id_consecutive_failures += 1
                        if item.device_id_consecutive_failures >= 2:
                            item.cooldown_until = time.time() + DEVICE_ID_FAIL_COOLDOWN_SECONDS
                            item.cooldown_reason = "device_id_failures"
                            item.last_error = "cooldown after repeated device_id failures"
                process = item.process
                if process is not None and process.poll() is not None:
                    item.disabled = True
                    item.last_error = f"process exited with code {process.poll()}"
                if (
                    success is False
                    and not item.disabled
                    and item.successes == 0
                    and item.consecutive_failures >= 3
                ):
                    item.disabled = True
                    item.last_error = "disabled after repeated proxy-stage failures"
                    if process is not None and process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=2)
                    self._spawn_next_node()
                self._cond.notify_all()
                return

    def close(self) -> None:
        with self._cond:
            for item in self._managed:
                process = item.process
                if process is None:
                    continue
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
                item.in_use = False
            self._started = False
            self._cond.notify_all()

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "name": item.node.name,
                    "region": item.node.region,
                    "proxy_url": item.proxy_url,
                    "local_port": item.local_port,
                    "in_use": item.in_use,
                    "disabled": item.disabled,
                    "successes": item.successes,
                    "failures": item.failures,
                    "consecutive_failures": item.consecutive_failures,
                    "device_id_successes": item.device_id_successes,
                    "device_id_failures": item.device_id_failures,
                    "device_id_consecutive_failures": item.device_id_consecutive_failures,
                    "cooldown_until": (
                        datetime.fromtimestamp(item.cooldown_until).isoformat(timespec="seconds")
                        if item.cooldown_until
                        else None
                    ),
                    "cooldown_reason": item.cooldown_reason,
                    "last_error": item.last_error,
                }
                for item in self._managed
            ]

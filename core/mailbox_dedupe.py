"""Local mailbox dedupe store for cfmail-style disposable addresses."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
import threading


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9@._+-]+", "_", str(value or "").strip())
    return cleaned.strip("._") or "mailbox"


def _normalize_email(email: str) -> str:
    return str(email or "").strip().lower()


@dataclass(frozen=True)
class MailboxDedupeEvent:
    timestamp: str
    action: str
    email: str
    reason: str = ""


class MailboxDedupeStore:
    def __init__(self, *, state_file: Path, pool_dir: Path) -> None:
        self.state_file = Path(state_file)
        self.pool_dir = Path(pool_dir)
        self._lock = threading.RLock()
        self._loaded = False
        self._seen: set[str] = set()
        self._inflight: set[str] = set()

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        if self.state_file.exists():
            for raw_line in self.state_file.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                email = _normalize_email(str(payload.get("email") or ""))
                if email:
                    self._seen.add(email)
        self._loaded = True

    def _pool_file_exists(self, email: str) -> bool:
        target = self.pool_dir / f"{_safe_component(email)}.json"
        return target.exists()

    def _append_event(self, action: str, email: str, *, reason: str = "") -> None:
        event = MailboxDedupeEvent(
            timestamp=datetime.now().astimezone().isoformat(timespec="seconds"),
            action=action,
            email=email,
            reason=reason,
        )
        with self.state_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.__dict__, ensure_ascii=False) + "\n")

    def reserve(self, email: str) -> bool:
        normalized = _normalize_email(email)
        if not normalized:
            return False
        with self._lock:
            self._ensure_loaded()
            if normalized in self._inflight or normalized in self._seen or self._pool_file_exists(normalized):
                self._seen.add(normalized)
                return False
            self._seen.add(normalized)
            self._inflight.add(normalized)
            self._append_event("reserve", normalized)
            return True

    def release(self, email: str) -> None:
        normalized = _normalize_email(email)
        if not normalized:
            return
        with self._lock:
            self._inflight.discard(normalized)

    def mark(self, email: str, *, reason: str) -> None:
        normalized = _normalize_email(email)
        if not normalized:
            return
        with self._lock:
            self._ensure_loaded()
            self._seen.add(normalized)
            self._append_event("mark", normalized, reason=reason)


_STORE_CACHE: dict[tuple[str, str], MailboxDedupeStore] = {}
_STORE_CACHE_LOCK = threading.Lock()


def get_mailbox_dedupe_store(*, state_file: Path, pool_dir: Path) -> MailboxDedupeStore:
    resolved_state_file = Path(state_file).expanduser().resolve()
    resolved_pool_dir = Path(pool_dir).expanduser().resolve()
    key = (str(resolved_state_file), str(resolved_pool_dir))
    with _STORE_CACHE_LOCK:
        store = _STORE_CACHE.get(key)
        if store is None:
            store = MailboxDedupeStore(state_file=resolved_state_file, pool_dir=resolved_pool_dir)
            _STORE_CACHE[key] = store
        return store

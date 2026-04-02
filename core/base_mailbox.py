"""Mailbox abstractions for zhuce6."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MailboxAccount:
    email: str
    account_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class BaseMailbox(ABC):
    @abstractmethod
    def get_email(self) -> MailboxAccount:
        """Create or reserve an email inbox."""

    @abstractmethod
    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set[str] | None = None,
    ) -> str:
        """Poll for a 6-digit verification code."""

    @abstractmethod
    def get_current_ids(self, account: MailboxAccount) -> set[str]:
        """Return the currently visible message ids."""


def create_mailbox(provider: str, proxy: str | None = None) -> BaseMailbox:
    provider_key = str(provider or "").strip().lower()
    if provider_key != "cfmail":
        raise ValueError(f"Unsupported mailbox provider: {provider}")

    from .cfmail import CfMailMailbox

    return CfMailMailbox(proxy=proxy)

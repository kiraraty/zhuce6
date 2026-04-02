"""Platform base types for zhuce6."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
import time


class AccountStatus(str, Enum):
    REGISTERED = "registered"
    TRIAL = "trial"
    SUBSCRIBED = "subscribed"
    EXPIRED = "expired"
    INVALID = "invalid"


@dataclass
class Account:
    platform: str
    email: str
    password: str
    user_id: str = ""
    region: str = ""
    token: str = ""
    status: AccountStatus = AccountStatus.REGISTERED
    trial_end_time: int = 0
    extra: dict[str, Any] = field(default_factory=dict)
    created_at: int = field(default_factory=lambda: int(time.time()))


@dataclass
class RegisterConfig:
    executor_type: str = "protocol"
    captcha_solver: str = "manual"
    proxy: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class BasePlatform(ABC):
    name: str = ""
    display_name: str = ""
    version: str = "1.0.0"

    def __init__(self, config: RegisterConfig | None = None) -> None:
        self.config = config or RegisterConfig()

    @abstractmethod
    def register(self, email: str | None = None, password: str | None = None) -> Account:
        """Execute the platform registration flow."""

    @abstractmethod
    def check_valid(self, account: Account) -> bool:
        """Check whether the account is currently valid."""

    def run_preflight(self, email: str | None = None, password: str | None = None) -> dict[str, Any]:
        del email, password
        raise NotImplementedError(f"Platform {self.name} does not expose a preflight flow")

    def exchange_callback(
        self,
        callback_url: str,
        expected_state: str,
        code_verifier: str,
        *,
        write_pool: bool = True,
        pool_dir: Path | None = None,
    ) -> dict[str, Any]:
        del callback_url, expected_state, code_verifier, write_pool, pool_dir
        raise NotImplementedError(f"Platform {self.name} does not expose a callback exchange flow")

    def get_platform_actions(self) -> list[dict[str, Any]]:
        return []

    def execute_action(self, action_id: str, account: Account, params: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError(f"Platform {self.name} does not support action: {action_id}")

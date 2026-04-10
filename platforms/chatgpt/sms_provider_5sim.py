"""5sim.net virtual number provider for phone OTP verification."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

FIVESIM_BASE_URL = "https://5sim.net/v1"
FIVESIM_PRODUCT = "openai"

OTP_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


class FiveSimProvider:
    """Client for 5sim.net virtual number API."""

    def __init__(
        self,
        api_key: str,
        country: str = "any",
        operator: str = "any",
        *,
        timeout: int = 15,
    ) -> None:
        self.api_key = api_key
        self.country = country
        self.operator = operator
        self.timeout = timeout

    def _request(self, method: str, path: str, max_retries: int = 3) -> Any:
        url = f"{FIVESIM_BASE_URL}{path}"
        last_exc: Exception | None = None
        for attempt in range(max_retries):
            try:
                req = Request(url, method=method)
                req.add_header("Authorization", f"Bearer {self.api_key}")
                req.add_header("Accept", "application/json")
                with urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read().decode("utf-8")
                try:
                    return json.loads(body)
                except Exception:
                    return body
            except Exception as exc:
                last_exc = exc
                if attempt < max_retries - 1:
                    time.sleep(1)
                    continue
                raise
        raise last_exc  # type: ignore[misc]

    def get_balance(self) -> float:
        result = self._request("GET", "/user/profile")
        if isinstance(result, dict):
            return float(result.get("balance", 0))
        raise RuntimeError(f"5sim: unexpected profile response: {result!r}")

    def buy_number(self) -> tuple[str, str]:
        """Buy a number for OpenAI verification.

        Returns (order_id, phone_number_with_plus).
        """
        result = self._request(
            "GET",
            f"/user/buy/activation/{self.country}/{self.operator}/{FIVESIM_PRODUCT}",
        )
        if isinstance(result, dict) and "id" in result:
            order_id = str(result["id"])
            phone = str(result.get("phone", ""))
            if not phone.startswith("+"):
                phone = f"+{phone}"
            return order_id, phone
        error = result if isinstance(result, str) else str(result)
        raise RuntimeError(f"5sim: buy_number failed: {error}")

    def poll_code(
        self,
        order_id: str,
        *,
        timeout: int = 300,
        interval: int = 5,
    ) -> str | None:
        """Poll for SMS verification code.

        Returns the 6-digit code string, or None on timeout.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                result = self._request("GET", f"/user/check/{order_id}")
            except Exception:
                time.sleep(interval)
                continue
            if isinstance(result, dict):
                sms_list = result.get("sms") or []
                if isinstance(sms_list, list):
                    for sms in sms_list:
                        text = str(sms.get("text") or sms.get("code") or "")
                        match = OTP_CODE_RE.search(text)
                        if match:
                            return match.group(1)
                status = str(result.get("status", ""))
                if status in ("CANCELED", "TIMEOUT", "BANNED"):
                    return None
            time.sleep(interval)
        return None

    def complete(self, order_id: str) -> str:
        """Mark order as finished."""
        try:
            result = self._request("GET", f"/user/finish/{order_id}")
            return str(result)
        except Exception as e:
            return str(e)

    def cancel(self, order_id: str) -> str:
        """Cancel order."""
        try:
            result = self._request("GET", f"/user/cancel/{order_id}")
            return str(result)
        except Exception as e:
            return str(e)


class RotatingFiveSimProvider:
    """Wraps FiveSimProvider with automatic country rotation on poll timeout."""

    def __init__(
        self,
        api_key: str,
        countries: list[str],
        operator: str = "any",
        *,
        timeout: int = 15,
    ) -> None:
        if not countries:
            raise ValueError("countries list cannot be empty")
        self._countries = countries
        self._operator = operator
        self._timeout = timeout
        self._api_key = api_key
        self._idx = 0
        self._current: FiveSimProvider | None = None
        self._activate(self._countries[self._idx])

    def _activate(self, country: str) -> None:
        self._current = FiveSimProvider(
            api_key=self._api_key,
            country=country,
            operator=self._operator,
            timeout=self._timeout,
        )

    def get_balance(self) -> float:
        return self._current.get_balance()  # type: ignore[union-attr]

    def buy_number(self) -> tuple[str, str]:
        return self._current.buy_number()  # type: ignore[union-attr]

    def poll_code(
        self,
        activation_id: str,
        *,
        timeout: int = 300,
        interval: int = 5,
    ) -> str | None:
        result = self._current.poll_code(activation_id, timeout=timeout, interval=interval)  # type: ignore[union-attr]
        if result is not None:
            return result
        # timeout — rotate country for next buy
        self._idx = (self._idx + 1) % len(self._countries)
        next_country = self._countries[self._idx]
        self._activate(next_country)
        return None

    def complete(self, activation_id: str) -> str:
        return self._current.complete(activation_id)  # type: ignore[union-attr]

    def cancel(self, activation_id: str) -> str:
        return self._current.cancel(activation_id)  # type: ignore[union-attr]

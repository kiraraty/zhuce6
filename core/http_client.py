"""Shared HTTP client wrapper for zhuce6."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any

from curl_cffi import requests as cffi_requests
from curl_cffi.requests import Response, Session


@dataclass
class RequestConfig:
    timeout: int = 30
    max_retries: int = 3
    retry_delay: float = 1.0
    impersonate: str = "chrome120"
    verify_ssl: bool = True
    follow_redirects: bool = True


class HTTPClientError(Exception):
    """Raised when the wrapped HTTP client cannot complete a request."""


class HTTPClient:
    def __init__(
        self,
        proxy_url: str | None = None,
        config: RequestConfig | None = None,
        session: Session | None = None,
    ) -> None:
        self.proxy_url = proxy_url
        self.config = config or RequestConfig()
        self._session = session

    @property
    def proxies(self) -> dict[str, str] | None:
        if not self.proxy_url:
            return None
        return {"http": self.proxy_url, "https": self.proxy_url}

    @property
    def session(self) -> Session:
        if self._session is None:
            self._session = Session(
                proxies=self.proxies,
                impersonate=self.config.impersonate,
                verify=self.config.verify_ssl,
                timeout=self.config.timeout,
            )
        return self._session

    def request(self, method: str, url: str, **kwargs: Any) -> Response:
        kwargs.setdefault("timeout", self.config.timeout)
        kwargs.setdefault("allow_redirects", self.config.follow_redirects)
        if self.proxies and "proxies" not in kwargs:
            kwargs["proxies"] = self.proxies

        last_error: Exception | None = None
        for attempt in range(self.config.max_retries):
            try:
                return self.session.request(method, url, **kwargs)
            except Exception as exc:
                last_error = exc
                if attempt < self.config.max_retries - 1:
                    time.sleep(self.config.retry_delay * (attempt + 1))
                    continue
                break

        raise HTTPClientError(f"Request failed: {method} {url} - {last_error}")

    def get(self, url: str, **kwargs: Any) -> Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, data: Any = None, json: Any = None, **kwargs: Any) -> Response:
        return self.request("POST", url, data=data, json=json, **kwargs)

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

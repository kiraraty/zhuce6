"""Adapter for using Sub2API via the existing CPA-style interface."""

from __future__ import annotations

from .sub2api_client import Sub2ApiClient


class Sub2ApiAdapter:
    """把 Sub2ApiClient 适配成 CpaClient 兼容接口, 让 ops/ 代码透明切换."""

    def __init__(self, client: Sub2ApiClient):
        self.client = client
        self._name_to_id: dict[str, int] = {}

    def _cache_account(self, account: dict) -> None:
        name = str(account.get("name") or "").strip()
        account_id = account.get("id")
        if name and isinstance(account_id, int):
            self._name_to_id[name] = account_id

    def _content_from_account(self, account: dict | None) -> dict | None:
        if not isinstance(account, dict):
            return None
        credentials = account.get("credentials")
        if isinstance(credentials, dict):
            return credentials
        return None

    def health_check(self) -> bool:
        return self.client.health_check()

    def _iter_accounts(self) -> list[dict]:
        accounts: list[dict] = []
        page = 1
        while True:
            payload = self.client.list_accounts(platform="openai", page=page, page_size=100)
            items = payload.get("items") if isinstance(payload, dict) else []
            if not isinstance(items, list) or not items:
                break
            for item in items:
                if isinstance(item, dict):
                    accounts.append(item)
            pages = int(payload.get("pages") or page) if isinstance(payload, dict) else page
            if page >= pages:
                break
            page += 1
        return accounts

    def list_auth_files(self) -> list[dict]:
        result: list[dict] = []
        for item in self._iter_accounts():
            self._cache_account(item)
            content = self._content_from_account(item)
            if content is None:
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            result.append({"name": name, "content": content})
        return result

    def upload_auth_file(self, name: str, content: dict) -> bool:
        credentials = {
            key: value
            for key, value in {
                "refresh_token": content.get("refresh_token"),
                "access_token": content.get("access_token"),
                "email": content.get("email"),
            }.items()
            if value not in {None, ""}
        }
        account = self.client.create_account(name=name, platform="openai", type="oauth", credentials=credentials)
        self._cache_account(account)
        return True

    def delete_auth_file(self, name: str) -> bool:
        account_id = self._name_to_id.get(name)
        if account_id is None:
            self.list_auth_files()
            account_id = self._name_to_id.get(name)
        if account_id is None:
            return False
        ok = self.client.delete_account(account_id)
        if ok:
            self._name_to_id.pop(name, None)
        return ok

    def get_auth_file(self, name: str) -> dict | None:
        account_id = self._name_to_id.get(name)
        if account_id is None:
            self.list_auth_files()
            account_id = self._name_to_id.get(name)
        if account_id is None:
            return None
        account = self.client.get_account(account_id)
        return self._content_from_account(account)

    def count_auth_files(self) -> int:
        payload = self.client.list_accounts(platform="openai", page=1, page_size=1)
        return int(payload.get("total") or 0) if isinstance(payload, dict) else 0

from types import SimpleNamespace

from ops.sub2api_adapter import Sub2ApiAdapter
from ops.sub2api_client import Sub2ApiClient
from ops.common import create_backend_client


class StubSub2ApiClient:
    def __init__(self):
        self.deleted_ids: list[int] = []
        self.created_payloads: list[dict] = []
        self.account_by_id = {
            11: {
                "id": 11,
                "name": "alpha@example.com.json",
                "credentials": {"refresh_token": "rt-a", "access_token": "at-a", "email": "alpha@example.com"},
            }
        }

    def health_check(self) -> bool:
        return True

    def list_accounts(self, platform="openai", page=1, page_size=100):
        return {
            "items": [
                {
                    "id": 11,
                    "name": "alpha@example.com.json",
                    "credentials": {"refresh_token": "rt-a", "access_token": "at-a", "email": "alpha@example.com"},
                }
            ],
            "total": 1,
            "page": page,
            "page_size": page_size,
            "pages": 1,
        }

    def create_account(self, **payload):
        self.created_payloads.append(payload)
        return {"id": 12, **payload}

    def delete_account(self, account_id: int) -> bool:
        self.deleted_ids.append(account_id)
        return True

    def get_account(self, account_id: int):
        return self.account_by_id.get(account_id)


def test_list_auth_files_converts_format():
    adapter = Sub2ApiAdapter(StubSub2ApiClient())

    files = adapter.list_auth_files()

    assert files == [
        {
            "name": "alpha@example.com.json",
            "content": {
                "refresh_token": "rt-a",
                "access_token": "at-a",
                "email": "alpha@example.com",
            },
        }
    ]


def test_upload_auth_file_creates_oauth_account():
    client = StubSub2ApiClient()
    adapter = Sub2ApiAdapter(client)

    ok = adapter.upload_auth_file(
        "beta@example.com.json",
        {"refresh_token": "rt-b", "access_token": "at-b", "email": "beta@example.com"},
    )

    assert ok is True
    assert client.created_payloads == [
        {
            "name": "beta@example.com.json",
            "platform": "openai",
            "type": "oauth",
            "credentials": {
                "refresh_token": "rt-b",
                "access_token": "at-b",
                "email": "beta@example.com",
            },
        }
    ]


def test_delete_auth_file_uses_name_to_id_cache():
    client = StubSub2ApiClient()
    adapter = Sub2ApiAdapter(client)
    adapter.list_auth_files()

    ok = adapter.delete_auth_file("alpha@example.com.json")

    assert ok is True
    assert client.deleted_ids == [11]


def test_count_auth_files():
    adapter = Sub2ApiAdapter(StubSub2ApiClient())

    assert adapter.count_auth_files() == 1


def test_create_backend_client_returns_sub2api_adapter(monkeypatch):
    settings = SimpleNamespace(
        backend="sub2api",
        sub2api_base_url="http://127.0.0.1:8080",
        sub2api_admin_email="admin@example.com",
        sub2api_admin_password="secret",
        sub2api_api_key="",
        cpa_management_base_url="http://unused",
    )

    client = create_backend_client(settings)

    assert isinstance(client, Sub2ApiAdapter)
    assert isinstance(client.client, Sub2ApiClient)
    assert client.client.base_url == "http://127.0.0.1:8080"

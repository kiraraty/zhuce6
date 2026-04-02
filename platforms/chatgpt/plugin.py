"""ChatGPT platform plugin for zhuce6."""

from __future__ import annotations

from datetime import datetime
import random
import string
from pathlib import Path
from typing import Any

from core.base_mailbox import BaseMailbox, create_mailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.mailbox_dedupe import get_mailbox_dedupe_store
from core.registry import register


class MailboxEmailServiceAdapter:
    def __init__(self, mailbox: BaseMailbox) -> None:
        self.mailbox = mailbox
        self._account = None

    def create_email(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        del config
        self._account = self.mailbox.get_email()
        return {
            "email": self._account.email,
            "service_id": self._account.account_id,
            "token": self._account.account_id,
        }

    def get_verification_code(
        self,
        email: str | None = None,
        email_id: str | None = None,
        timeout: int = 120,
        pattern: str | None = None,
        otp_sent_at: float | None = None,
    ) -> str:
        del email, email_id, pattern, otp_sent_at
        if self._account is None:
            return ""
        return self.mailbox.wait_for_code(self._account, keyword="", timeout=timeout)


@register
class ChatGPTPlatform(BasePlatform):
    name = "chatgpt"
    display_name = "ChatGPT"
    version = "0.1.0"

    def __init__(self, config: RegisterConfig | None = None, mailbox: BaseMailbox | None = None) -> None:
        super().__init__(config)
        self.mailbox = mailbox

    def check_valid(self, account: Account) -> bool:
        try:
            from platforms.chatgpt.payment import check_subscription_status

            class _AccountView:
                pass

            view = _AccountView()
            extra = account.extra or {}
            view.access_token = extra.get("access_token") or account.token
            view.cookies = extra.get("cookies", "")
            status = check_subscription_status(view, proxy=self.config.proxy if self.config else None)
            return status not in ("expired", "invalid", "banned", None)
        except Exception:
            return False

    def _resolve_mail_provider(self) -> str:
        return str((self.config.extra or {}).get("mail_provider", "cfmail")).strip() or "cfmail"

    def _resolve_mailbox(self, provider_name: str) -> BaseMailbox:
        if self.mailbox is not None:
            return self.mailbox
        return create_mailbox(provider_name, proxy=self.config.proxy if self.config else None)

    def _run_registration(self, email: str | None = None, password: str | None = None) -> dict[str, Any]:
        from platforms.chatgpt.register import RegistrationEngine

        provider_name = self._resolve_mail_provider()
        mailbox = self._resolve_mailbox(provider_name)
        mailbox_dedupe_store = get_mailbox_dedupe_store(
            state_file=Path.cwd() / "state" / "seen_mailboxes.jsonl",
            pool_dir=self.config.output_dir if self.config and self.config.output_dir else Path.cwd() / "pool",
        )
        engine = RegistrationEngine(
            email_service=MailboxEmailServiceAdapter(mailbox),
            proxy_url=self.config.proxy if self.config else None,
            mailbox_dedupe_store=mailbox_dedupe_store,
        )
        if email:
            engine.email = email
        engine.password = password
        result = engine.run()
        payload = result.to_dict()
        metadata = payload.setdefault("metadata", {})
        metadata["mail_provider"] = provider_name
        return payload

    def run_preflight(self, email: str | None = None, password: str | None = None) -> dict[str, Any]:
        from platforms.chatgpt.register import RegistrationEngine

        provider_name = self._resolve_mail_provider()
        mailbox = self._resolve_mailbox(provider_name)
        mailbox_dedupe_store = get_mailbox_dedupe_store(
            state_file=Path.cwd() / "state" / "seen_mailboxes.jsonl",
            pool_dir=self.config.output_dir if self.config and self.config.output_dir else Path.cwd() / "pool",
        )
        engine = RegistrationEngine(
            email_service=MailboxEmailServiceAdapter(mailbox),
            proxy_url=self.config.proxy if self.config else None,
            mailbox_dedupe_store=mailbox_dedupe_store,
        )
        if email:
            engine.email = email
        engine.password = password
        result = engine.run_preflight()
        payload = result.to_dict()
        metadata = payload.setdefault("metadata", {})
        metadata["mail_provider"] = provider_name
        return payload

    def run_register_once(
        self,
        email: str | None = None,
        password: str | None = None,
        *,
        write_pool: bool = True,
        pool_dir: Path | None = None,
    ) -> dict[str, Any]:
        from platforms.chatgpt.pool import write_token_record
        from platforms.chatgpt.register import RegistrationEngine

        provider_name = self._resolve_mail_provider()
        mailbox = self._resolve_mailbox(provider_name)
        target_pool_dir = pool_dir or Path.cwd() / "pool"
        mailbox_dedupe_store = get_mailbox_dedupe_store(
            state_file=Path.cwd() / "state" / "seen_mailboxes.jsonl",
            pool_dir=target_pool_dir,
        )
        engine = RegistrationEngine(
            email_service=MailboxEmailServiceAdapter(mailbox),
            proxy_url=self.config.proxy if self.config else None,
            mailbox_dedupe_store=mailbox_dedupe_store,
        )
        if email:
            engine.email = email
        engine.password = password
        result = engine.run()
        payload = result.to_dict()
        metadata = payload.setdefault("metadata", {})
        metadata["mail_provider"] = provider_name
        if result.success and write_pool:
            mailbox_account = getattr(adapter := engine.email_service, "_account", None)
            mailbox_payload = {
                "email": result.email,
                "account_id": "",
                "extra": {},
            }
            if mailbox_account is not None:
                mailbox_payload = {
                    "email": str(getattr(mailbox_account, "email", "") or result.email).strip() or result.email,
                    "account_id": str(getattr(mailbox_account, "account_id", "") or "").strip(),
                    "extra": dict(getattr(mailbox_account, "extra", {}) or {}),
                }
            token_data = {
                "type": "codex",
                "email": result.email,
                "password": result.password,
                "mail_provider": provider_name,
                "mailbox": mailbox_payload,
                "expired": metadata.get("expired") or "",
                "id_token": result.id_token,
                "account_id": result.account_id,
                "access_token": result.access_token,
                "last_refresh": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "refresh_token": result.refresh_token,
            }
            written_path = write_token_record(token_data, target_pool_dir)
            payload["pool_file"] = str(written_path)
            payload["written_to_pool"] = True
        else:
            payload["pool_file"] = ""
            payload["written_to_pool"] = False
        return payload

    def exchange_callback(
        self,
        callback_url: str,
        expected_state: str,
        code_verifier: str,
        *,
        write_pool: bool = True,
        pool_dir: Path | None = None,
    ) -> dict[str, Any]:
        from platforms.chatgpt.oauth import OAuthManager
        from platforms.chatgpt.pool import write_token_record

        try:
            token_data = OAuthManager(proxy_url=self.config.proxy if self.config else None).handle_callback(
                callback_url=callback_url,
                expected_state=expected_state,
                code_verifier=code_verifier,
            )
            pool_file = ""
            if write_pool:
                target_dir = pool_dir or Path.cwd() / "pool"
                written_path = write_token_record(token_data, target_dir)
                pool_file = str(written_path)
            return {
                "success": True,
                "stage": "oauth_callback_exchanged",
                "email": str(token_data.get("email") or ""),
                "account_id": str(token_data.get("account_id") or ""),
                "written_to_pool": write_pool,
                "pool_file": pool_file,
                "token_data": token_data,
                "source": "callback_exchange",
            }
        except Exception as exc:
            return {
                "success": False,
                "stage": "oauth_callback_exchange",
                "error_message": str(exc),
                "written_to_pool": False,
                "pool_file": "",
                "token_data": {},
                "source": "callback_exchange",
            }

    def register(self, email: str | None = None, password: str | None = None) -> Account:
        if not password:
            password = "".join(random.choices(string.ascii_letters + string.digits + "!@#$", k=16))
        payload = self._run_registration(email=email, password=password)
        if not payload.get("success"):
            raise RuntimeError(str(payload.get("error_message") or "registration flow failed"))

        return Account(
            platform="chatgpt",
            email=str(payload.get("email") or ""),
            password=str(payload.get("password") or password),
            user_id=str(payload.get("account_id") or ""),
            token=str(payload.get("access_token") or ""),
            status=AccountStatus.REGISTERED,
            extra={
                "access_token": payload.get("access_token", ""),
                "refresh_token": payload.get("refresh_token", ""),
                "id_token": payload.get("id_token", ""),
                "session_token": payload.get("session_token", ""),
                "workspace_id": payload.get("workspace_id", ""),
            },
        )

    def get_platform_actions(self) -> list[dict[str, Any]]:
        return [
            {"id": "refresh_token", "label": "Refresh token", "params": []},
            {
                "id": "payment_link",
                "label": "Generate payment link",
                "params": [
                    {"key": "country", "label": "Country", "type": "select", "options": ["US", "SG", "TR", "HK"]},
                    {"key": "plan", "label": "Plan", "type": "select", "options": ["plus", "team"]},
                ],
            },
            {
                "id": "upload_cpa",
                "label": "Upload CPA",
                "params": [
                    {"key": "api_url", "label": "CPA API URL", "type": "text"},
                    {"key": "api_key", "label": "CPA API key", "type": "text"},
                ],
            },
        ]

    def execute_action(self, action_id: str, account: Account, params: dict[str, Any]) -> dict[str, Any]:
        proxy = self.config.proxy if self.config else None
        extra = account.extra or {}

        class _AccountView:
            pass

        view = _AccountView()
        view.email = account.email
        view.access_token = extra.get("access_token") or account.token
        view.refresh_token = extra.get("refresh_token", "")
        view.session_token = extra.get("session_token", "")
        view.client_id = extra.get("client_id", "app_EMoamEEZ73f0CkXaXp7hrann")
        view.cookies = extra.get("cookies", "")
        view.id_token = extra.get("id_token", "")
        view.account_id = extra.get("account_id", account.user_id)
        view.last_refresh = extra.get("last_refresh")
        view.expires_at = extra.get("expires_at")

        if action_id == "refresh_token":
            from platforms.chatgpt.token_refresh import TokenRefreshManager

            result = TokenRefreshManager(proxy_url=proxy).refresh_account(view)
            if result.success:
                return {
                    "ok": True,
                    "data": {
                        "access_token": result.access_token,
                        "refresh_token": result.refresh_token,
                    },
                }
            return {"ok": False, "error": result.error_message}

        if action_id == "payment_link":
            from platforms.chatgpt.payment import generate_plus_link, generate_team_link

            plan = params.get("plan", "plus")
            country = params.get("country", "US")
            url = generate_plus_link(view, proxy=proxy, country=country)
            if plan == "team":
                url = generate_team_link(view, proxy=proxy, country=country)
            return {"ok": bool(url), "data": {"url": url}}

        if action_id == "upload_cpa":
            from platforms.chatgpt.cpa_upload import generate_token_json, upload_to_cpa

            ok, message = upload_to_cpa(
                generate_token_json(view),
                api_url=params.get("api_url"),
                api_key=params.get("api_key"),
            )
            return {"ok": ok, "data": message}

        raise NotImplementedError(f"Unknown action: {action_id}")

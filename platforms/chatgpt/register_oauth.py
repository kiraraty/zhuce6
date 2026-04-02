"""OAuth helpers for ChatGPT registration."""

from __future__ import annotations

import base64
from datetime import datetime
import json
import re
import time
import urllib.parse
from typing import Any

from .constants import OPENAI_API_ENDPOINTS, OPENAI_PAGE_TYPES
from .http_client import OpenAIHTTPClient
from .oauth import submit_callback_url
from .token_refresh import TokenRefreshManager


def _extract_callback_url(self, value: str) -> str | None:
    candidate = str(value or "").strip()
    if not candidate:
        return None
    candidate = self._auth_url(candidate)
    parsed = urllib.parse.urlparse(candidate)
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    code = str((query.get("code") or [""])[0] or "").strip()
    state = str((query.get("state") or [""])[0] or "").strip()
    if code and state:
        return candidate
    return None

def _extract_callback_url_from_error(self, exc: Exception) -> str | None:
    matched = re.search(r"(https?://localhost[^\s'\"\\]+)", str(exc))
    if not matched:
        return None
    return self._extract_callback_url(matched.group(1))

def _extract_session_token(self, session: Any | None = None) -> str | None:
    target_session = session or self.session
    if target_session is None:
        return None
    cookies = getattr(target_session, "cookies", None)
    if cookies is None:
        return None
    for cookie_name in ("__Secure-next-auth.session-token", "next-auth.session-token"):
        try:
            cookie_value = str(cookies.get(cookie_name) or "").strip()
        except Exception:
            cookie_value = ""
        if cookie_value:
            return cookie_value
    jar = getattr(cookies, "jar", None)
    if jar is None:
        return None
    for item in list(jar):
        name = str(getattr(item, "name", "") or "").strip()
        if name not in {"__Secure-next-auth.session-token", "next-auth.session-token"}:
            continue
        value = str(getattr(item, "value", "") or "").strip()
        if value:
            return value
    return None

def _refresh_tokens_from_session_cookie(
    self,
    session: Any | None = None,
    *,
    label: str,
) -> dict[str, Any] | None:
    session_token = self._extract_session_token(session)
    if not session_token:
        self._log(f"{label}: session token missing")
        return None
    self._log(f"{label}: session token detected")
    refresh_result = TokenRefreshManager(proxy_url=self.proxy_url).refresh_by_session_token(session_token)
    if not refresh_result.success:
        self._log(f"{label}: session token refresh failed: {refresh_result.error_message}")
        return None
    self._log(f"{label}: session token refresh succeeded")
    expired = ""
    if refresh_result.expires_at is not None:
        expired = refresh_result.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "access_token": refresh_result.access_token,
        "refresh_token": refresh_result.refresh_token,
        "id_token": "",
        "account_id": refresh_result.account_id,
        "email": refresh_result.email or str(self.email or "").strip(),
        "expired": expired,
        "last_refresh": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session_token": refresh_result.session_token or session_token,
    }

def _decode_oauth_session_cookie(self, session: Any | None = None) -> dict[str, Any] | None:
    target_session = session or self.session
    if target_session is None:
        return None
    cookies = getattr(target_session, "cookies", None)
    if cookies is None:
        return None
    jar = getattr(cookies, "jar", None)
    cookie_items = list(jar) if jar is not None else []
    raw_cookie = str(cookies.get("oai-client-auth-session") or "").strip()
    if raw_cookie:
        cookie_items.insert(0, type("CookieItem", (), {"name": "oai-client-auth-session", "value": raw_cookie})())
    for item in cookie_items:
        name = str(getattr(item, "name", "") or "").strip()
        if "oai-client-auth-session" not in name:
            continue
        raw_value = str(getattr(item, "value", "") or "").strip()
        if not raw_value:
            continue
        candidates = [raw_value]
        try:
            from urllib.parse import unquote

            decoded = unquote(raw_value)
            if decoded != raw_value:
                candidates.append(decoded)
        except Exception:
            pass
        for candidate in candidates:
            try:
                value = candidate
                if (value.startswith('"') and value.endswith('"')) or (
                    value.startswith("'") and value.endswith("'")
                ):
                    value = value[1:-1]
                part = value.split(".")[0] if "." in value else value
                pad = "=" * ((4 - (len(part) % 4)) % 4)
                decoded = base64.urlsafe_b64decode((part + pad).encode("ascii"))
                data = json.loads(decoded.decode("utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                continue
    return None

def _login_for_token(self) -> dict[str, Any] | None:
    """Login with email+password in a fresh session to obtain OAuth tokens."""
    if not self.email or not self.password:
        self._log("login_for_token: email or password missing")
        return None

    try:
        login_client = OpenAIHTTPClient(proxy_url=self.proxy_url)
        login_session = login_client.session
        login_oauth = self.oauth_manager.start_oauth()
        self._log("login_for_token: new oauth flow started")
        authorize_params = dict(
            urllib.parse.parse_qsl(
                urllib.parse.urlparse(login_oauth.auth_url).query,
                keep_blank_values=True,
            )
        )

        def _refresh_login_session() -> Any:
            nonlocal login_client, login_session
            cookie_pairs: dict[str, str] = {}
            try:
                cookies = getattr(login_session, "cookies", None)
                if cookies is not None:
                    jar = getattr(cookies, "jar", None)
                    if jar is not None:
                        for item in list(jar):
                            name = str(getattr(item, "name", "") or "").strip()
                            value = str(getattr(item, "value", "") or "").strip()
                            if name:
                                cookie_pairs[name] = value
                    for key, value in dict(cookies).items():
                        if key:
                            cookie_pairs[str(key)] = str(value)
            except Exception:
                pass
            try:
                login_client.close()
            except Exception:
                pass
            login_client = OpenAIHTTPClient(proxy_url=self.proxy_url)
            login_session = login_client.session
            try:
                login_session.cookies.update(cookie_pairs)
            except Exception:
                pass
            return login_session

        def _bootstrap_oauth_session() -> str:
            nonlocal login_session
            response, login_session = self._session_request(
                session=login_session,
                method="GET",
                url=login_oauth.auth_url,
                label="login_for_token: oauth bootstrap",
                refresh_session=_refresh_login_session,
                timeout=15,
                allow_redirects=True,
            )
            final_url = str(getattr(response, "url", "") or login_oauth.auth_url)
            has_login_session = bool(str(login_session.cookies.get("login_session") or "").strip())
            if not has_login_session:
                response, login_session = self._session_request(
                    session=login_session,
                    method="GET",
                    url="https://auth.openai.com/api/oauth/oauth2/auth",
                    label="login_for_token: oauth bootstrap fallback",
                    refresh_session=_refresh_login_session,
                    params=authorize_params,
                    timeout=15,
                    allow_redirects=True,
                )
                final_url = str(getattr(response, "url", "") or final_url)
            return final_url

        def _resolve_callback_from_response(response: Any, referer: str) -> str | None:
            location = self._auth_url(str(response.headers.get("Location") or "").strip())
            if response.status_code in {301, 302, 303, 307, 308} and location:
                self._log(f"login_for_token: redirect -> {location[:120]}")
                return self._extract_callback_url(location) or self._follow_redirects_with_session(
                    login_session,
                    location,
                    referer=referer,
                )
            return None

        authorize_final_url = _bootstrap_oauth_session()
        device_id = str(login_session.cookies.get("oai-did") or "").strip()
        self._log(f"login_for_token: device_id={'yes' if device_id else 'no'}")
        if not device_id:
            self._log("login_for_token: missing device_id after oauth bootstrap")
            return None

        authorize_sentinel = login_client.check_sentinel(device_id, flow="authorize_continue")
        if not authorize_sentinel:
            self._log("login_for_token: authorize sentinel unavailable")
            return None

        continue_referer = authorize_final_url if authorize_final_url.startswith("https://auth.openai.com") else "https://auth.openai.com/log-in"
        login_headers = self._oauth_json_headers(referer=continue_referer, device_id=device_id)
        login_headers["openai-sentinel-token"] = self._build_sentinel_header(
            authorize_sentinel,
            device_id,
            "authorize_continue",
            client=login_client,
        )
        login_resp, login_session = self._session_request(
            session=login_session,
            method="POST",
            url=OPENAI_API_ENDPOINTS["signup"],
            label="login_for_token: authorize continue",
            refresh_session=_refresh_login_session,
            headers=login_headers,
            json={"username": {"kind": "email", "value": self.email}},
            timeout=15,
            allow_redirects=False,
        )
        self._log(f"login_for_token: authorize continue status={login_resp.status_code}")
        if login_resp.status_code == 400 and "invalid_auth_step" in (login_resp.text or ""):
            authorize_final_url = _bootstrap_oauth_session()
            continue_referer = authorize_final_url if authorize_final_url.startswith("https://auth.openai.com") else "https://auth.openai.com/log-in"
            login_headers = self._oauth_json_headers(referer=continue_referer, device_id=device_id)
            login_headers["openai-sentinel-token"] = self._build_sentinel_header(
                authorize_sentinel,
                device_id,
                "authorize_continue",
                client=login_client,
            )
            login_resp, login_session = self._session_request(
                session=login_session,
                method="POST",
                url=OPENAI_API_ENDPOINTS["signup"],
                label="login_for_token: authorize continue retry",
                refresh_session=_refresh_login_session,
                headers=login_headers,
                json={"username": {"kind": "email", "value": self.email}},
                timeout=15,
                allow_redirects=False,
            )
            self._log(f"login_for_token: authorize continue retry status={login_resp.status_code}")
        if login_resp.status_code != 200:
            self._log(f"login_for_token: authorize continue body={login_resp.text[:240]}")
            return None

        try:
            login_data = login_resp.json()
        except Exception as exc:
            self._log(f"login_for_token: authorize continue parse failed: {exc}")
            return None

        continue_url = self._auth_url(str(login_data.get("continue_url") or "").strip())
        page_type = str(((login_data.get("page") or {}).get("type")) or "").strip()

        password_sentinel = login_client.check_sentinel(device_id, flow="password_verify")
        if not password_sentinel:
            self._log("login_for_token: password sentinel unavailable")
            return None

        oauth_otp_before_ids = self._capture_mailbox_ids()
        self._log(f"login_for_token: oauth otp baseline ids={len(oauth_otp_before_ids)}")
        password_headers = self._oauth_json_headers(
            referer="https://auth.openai.com/log-in/password",
            device_id=device_id,
        )
        password_headers["openai-sentinel-token"] = self._build_sentinel_header(
            password_sentinel,
            device_id,
            "password_verify",
            client=login_client,
        )
        pw_resp, login_session = self._session_request(
            session=login_session,
            method="POST",
            url=OPENAI_API_ENDPOINTS["password_verify"],
            label="login_for_token: password verify",
            refresh_session=_refresh_login_session,
            headers=password_headers,
            json={"password": self.password},
            timeout=15,
            allow_redirects=False,
        )
        self._log(f"login_for_token: password verify status={pw_resp.status_code}")
        if pw_resp.status_code != 200:
            self._log(f"login_for_token: password verify body={pw_resp.text[:500]}")
            return None

        try:
            pw_data = pw_resp.json()
        except Exception as exc:
            self._log(f"login_for_token: password verify parse failed: {exc}")
            return None

        continue_url = self._auth_url(str(pw_data.get("continue_url") or continue_url or "").strip())
        page_type = str(((pw_data.get("page") or {}).get("type")) or page_type or "").strip()
        self._log(
            "login_for_token: continue_url="
            f"{continue_url[:120] if continue_url else 'none'}, page={page_type or 'unknown'}"
        )

        need_oauth_otp = (
            page_type == OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]
            or "email-verification" in (continue_url or "")
            or "email-otp" in (continue_url or "")
        )
        if need_oauth_otp:
            self._log("login_for_token: oauth email verification required")
            # Solution C: Try session cookie refresh BEFORE OTP wait.
            # After password_verify, the login session may already have a
            # usable session cookie, letting us skip the second OTP entirely.
            try:
                pre_otp_token = self._refresh_tokens_from_session_cookie(
                    login_session, label="login_for_token:pre-otp-session"
                )
                if pre_otp_token:
                    self._log("login_for_token: session cookie refresh bypassed OTP requirement")
                    return pre_otp_token
            except Exception as exc:
                self._log(f"login_for_token: pre-otp session refresh failed: {exc}")
            otp_code = self._wait_for_mailbox_code(
                before_ids=oauth_otp_before_ids,
                timeout=self._add_phone_oauth_otp_timeout_seconds,
                keyword="openai",
            )
            mailbox, _acct = self._mailbox_context()
            if mailbox is not None:
                diag = dict(getattr(mailbox, "last_wait_diagnostics", {}) or {})
                self._log(
                    f"login_for_token: oauth otp diagnostics: "
                    f"polls={diag.get('poll_count', '?')} "
                    f"scanned={diag.get('message_scan_count', '?')} "
                    f"first_seen_after={diag.get('first_message_seen_at', '-')}"
                )
            if not otp_code:
                self._log("login_for_token: oauth email verification code not received")
                return None
            self._log(f"login_for_token: oauth otp received {otp_code}")
            otp_headers = self._oauth_json_headers(
                referer="https://auth.openai.com/email-verification",
                device_id=device_id,
            )
            otp_resp, login_session = self._session_request(
                session=login_session,
                method="POST",
                url=OPENAI_API_ENDPOINTS["validate_otp"],
                label="login_for_token: oauth otp validate",
                refresh_session=_refresh_login_session,
                headers=otp_headers,
                json={"code": otp_code},
                timeout=15,
                allow_redirects=False,
            )
            self._log(f"login_for_token: oauth otp validate status={otp_resp.status_code}")
            if otp_resp.status_code != 200:
                self._log(f"login_for_token: oauth otp validate body={otp_resp.text[:500]}")
                return None
            try:
                otp_data = otp_resp.json()
            except Exception as exc:
                self._log(f"login_for_token: oauth otp parse failed: {exc}")
                return None
            continue_url = self._auth_url(str(otp_data.get("continue_url") or continue_url or "").strip())
            page_type = str(((otp_data.get("page") or {}).get("type")) or page_type or "").strip()
            self._log(
                "login_for_token: oauth otp continue_url="
                f"{continue_url[:120] if continue_url else 'none'}, page={page_type or 'unknown'}"
            )

        callback_url = self._extract_callback_url(continue_url)
        if not callback_url and continue_url:
            callback_url = self._follow_redirects_with_session(
                login_session,
                continue_url,
                referer="https://auth.openai.com/log-in/password",
            )

        consent_hint = any(
            hint
            for hint in (
                "consent" in (continue_url or ""),
                "workspace" in (continue_url or ""),
                "organization" in (continue_url or ""),
                "consent" in page_type,
                "organization" in page_type,
            )
        )
        if not callback_url and consent_hint:
            session_data = self._decode_oauth_session_cookie(login_session) or {}
            workspaces = session_data.get("workspaces") or []
            workspace_id = ""
            if workspaces and isinstance(workspaces, list):
                workspace_id = str((workspaces[0] or {}).get("id") or "").strip()
            if not workspace_id:
                self._log("login_for_token: workspace id missing in oauth session cookie")
            else:
                workspace_referer = continue_url or "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"
                workspace_headers = self._oauth_json_headers(
                    referer=workspace_referer,
                    device_id=device_id,
                )
                ws_resp, login_session = self._session_request(
                    session=login_session,
                    method="POST",
                    url=OPENAI_API_ENDPOINTS["select_workspace"],
                    label="login_for_token: workspace select",
                    refresh_session=_refresh_login_session,
                    headers=workspace_headers,
                    json={"workspace_id": workspace_id},
                    timeout=15,
                    allow_redirects=False,
                )
                self._log(f"login_for_token: workspace select status={ws_resp.status_code}")
                callback_url = _resolve_callback_from_response(ws_resp, workspace_referer)
                if not callback_url and ws_resp.status_code == 200:
                    try:
                        ws_data = ws_resp.json()
                    except Exception as exc:
                        self._log(f"login_for_token: workspace select parse failed: {exc}")
                        ws_data = {}
                    ws_next = self._auth_url(str(ws_data.get("continue_url") or "").strip())
                    orgs = ((ws_data.get("data") or {}).get("orgs") or []) if isinstance(ws_data, dict) else []
                    self._log(
                        "login_for_token: workspace select continue_url="
                        f"{ws_next[:120] if ws_next else 'none'}, org_count={len(orgs) if isinstance(orgs, list) else 0}"
                    )
                    if orgs and isinstance(orgs, list):
                        first_org = orgs[0] or {}
                        org_id = str(first_org.get("id") or "").strip()
                        projects = first_org.get("projects") or []
                        project_id = ""
                        if projects and isinstance(projects, list):
                            project_id = str((projects[0] or {}).get("id") or "").strip()
                        if org_id:
                            org_body = {"org_id": org_id}
                            if project_id:
                                org_body["project_id"] = project_id
                            org_referer = ws_next or workspace_referer
                            org_headers = self._oauth_json_headers(
                                referer=org_referer,
                                device_id=device_id,
                            )
                            org_resp, login_session = self._session_request(
                                session=login_session,
                                method="POST",
                                url=OPENAI_API_ENDPOINTS["select_organization"],
                                label="login_for_token: organization select",
                                refresh_session=_refresh_login_session,
                                headers=org_headers,
                                json=org_body,
                                timeout=15,
                                allow_redirects=False,
                            )
                            self._log(f"login_for_token: organization select status={org_resp.status_code}")
                            callback_url = _resolve_callback_from_response(org_resp, org_referer)
                            if not callback_url and org_resp.status_code == 200:
                                try:
                                    org_data = org_resp.json()
                                except Exception as exc:
                                    self._log(f"login_for_token: organization select parse failed: {exc}")
                                    org_data = {}
                                org_next = self._auth_url(str(org_data.get("continue_url") or "").strip())
                                self._log(
                                    "login_for_token: organization select continue_url="
                                    f"{org_next[:120] if org_next else 'none'}"
                                )
                                if org_next:
                                    callback_url = self._extract_callback_url(org_next) or self._follow_redirects_with_session(
                                        login_session,
                                        org_next,
                                        referer=org_referer,
                                    )
                            elif org_resp.status_code != 200:
                                self._log(
                                    "login_for_token: organization select body="
                                    f"{str(getattr(org_resp, 'text', '') or '')[:320]}"
                                )
                    if not callback_url and ws_next:
                        callback_url = self._extract_callback_url(ws_next) or self._follow_redirects_with_session(
                            login_session,
                            ws_next,
                            referer=workspace_referer,
                        )
                elif ws_resp.status_code != 200:
                    self._log(
                        "login_for_token: workspace select body="
                        f"{str(getattr(ws_resp, 'text', '') or '')[:320]}"
                    )

        if not callback_url:
            session_token_info = self._refresh_tokens_from_session_cookie(login_session, label="login_for_token")
            if session_token_info:
                return session_token_info
            self._log("login_for_token: could not obtain callback url")
            return None

        self._log(f"login_for_token: callback obtained, exchanging for tokens")
        try:
            token_resp = submit_callback_url(
                callback_url=callback_url,
                expected_state=login_oauth.state,
                code_verifier=login_oauth.code_verifier,
                redirect_uri=login_oauth.redirect_uri,
                proxy_url=self.proxy_url,
            )
            self._log("login_for_token: token exchange successful")
            parsed = self._parse_token_response(token_resp)
            if parsed is not None:
                session_token = self._extract_session_token(login_session)
                if session_token:
                    parsed["session_token"] = session_token
            return parsed
        except Exception as exc:
            self._log(f"login_for_token: token exchange failed: {exc}")
            return None

    except Exception as exc:
        self._log(f"login_for_token failed: {exc}")
        return None

def _follow_redirects_with_session(
    self,
    session: Any,
    url: str,
    max_hops: int = 12,
    referer: str | None = None,
) -> str | None:
    """Follow redirect chain with a specific session, looking for callback URL with code=."""
    current_url = self._auth_url(url)
    current_referer = referer
    for hop in range(1, max_hops + 1):
        direct_callback = self._extract_callback_url(current_url)
        if direct_callback:
            return direct_callback
        try:
            headers = {"referer": current_referer} if current_referer else None
            resp = session.get(
                current_url,
                timeout=15,
                allow_redirects=False,
                headers=headers,
            )
            response_url = self._auth_url(str(getattr(resp, "url", "") or current_url))
            direct_callback = self._extract_callback_url(response_url)
            if direct_callback:
                return direct_callback
            location = self._auth_url(str(resp.headers.get("Location") or "").strip())
            if resp.status_code in {301, 302, 303, 307, 308} and location:
                self._log(f"login redirect {hop}: {location[:100]}")
                direct_callback = self._extract_callback_url(location)
                if direct_callback:
                    return direct_callback
                current_referer = response_url or current_url
                current_url = location
            else:
                body_preview = ""
                try:
                    body_preview = str(getattr(resp, "text", "") or "")[:320]
                except Exception:
                    body_preview = ""
                self._log(
                    "login redirect chain ended at hop "
                    f"{hop} with status {resp.status_code}, url={response_url[:120]}, body={body_preview}"
                )
                return None
        except Exception as exc:
            callback_url = self._extract_callback_url_from_error(exc)
            if callback_url:
                return callback_url
            self._log(f"login redirect {hop} failed: {exc}")
            return None
    self._log("login redirect chain exceeded max hops")
    return None

def _parse_token_response(self, raw: Any) -> dict[str, Any] | None:
    """Parse token exchange response into standardized dict."""
    if not raw or not isinstance(raw, (dict, str)):
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    access_token = str(raw.get("access_token") or "").strip()
    if not access_token:
        return None
    return {
        "access_token": access_token,
        "refresh_token": str(raw.get("refresh_token") or "").strip(),
        "id_token": str(raw.get("id_token") or "").strip(),
        "account_id": str(raw.get("account_id") or "").strip(),
        "email": str(raw.get("email") or "").strip(),
        "expired": str(raw.get("expired") or "").strip(),
        "last_refresh": str(raw.get("last_refresh") or "").strip(),
    }

def _parse_workspace_from_cookie(self, session: Any | None = None) -> str | None:
    """Extract workspace id from oai-client-auth-session JWT cookie."""
    payload = self._decode_oauth_session_cookie(session)
    if not payload:
        self._log("oai-client-auth-session cookie missing or malformed")
        return None
    workspaces = payload.get("workspaces") or []
    if not workspaces:
        self._log(f"workspace list missing in auth session payload (keys: {list(payload.keys())})")
        return None
    workspace_id = str((workspaces[0] or {}).get("id") or "").strip()
    if not workspace_id:
        self._log("workspace id missing in auth session payload")
        return None
    self._log(f"workspace id acquired: {workspace_id}")
    return workspace_id

def _get_workspace_id(self) -> str | None:
    if self.session is None:
        return None
    try:
        workspace_id = self._parse_workspace_from_cookie()
        if workspace_id:
            return workspace_id

        self._log("workspace fallback: triggering authorize/continue")
        try:
            resp2 = self.session.get(
                OPENAI_API_ENDPOINTS["signup"],
                headers={
                    "referer": "https://auth.openai.com/create-account",
                    "accept": "application/json",
                },
                timeout=15,
                allow_redirects=True,
            )
            self._log(f"authorize/continue status: {resp2.status_code}")
        except Exception:
            pass
        time.sleep(1)

        # Try cookie one more time after authorize
        workspace_id = self._parse_workspace_from_cookie()
        if workspace_id:
            return workspace_id

        self._log("all workspace retrieval methods exhausted")
        return None
    except Exception as exc:
        self._log(f"get_workspace_id failed: {exc}")
        return None

def _select_workspace(self, workspace_id: str) -> str | None:
    if self.session is None:
        return None
    try:
        response = self.session.post(
            OPENAI_API_ENDPOINTS["select_workspace"],
            headers={
                "referer": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
                "content-type": "application/json",
            },
            data=json.dumps({"workspace_id": workspace_id}),
        )
        self._log(f"select workspace status: {response.status_code}")
        if response.status_code != 200:
            self._log(f"select workspace body: {response.text[:240]}")
            return None
        continue_url = str((response.json() or {}).get("continue_url") or "").strip()
        if continue_url:
            self._log("continue_url acquired")
            return continue_url
        self._log("continue_url missing from workspace selection")
        return None
    except Exception as exc:
        self._log(f"select_workspace failed: {exc}")
        return None

# ── Solution D: direct session token extraction ──────────────────

def _try_create_account_callback_session_token(self, continue_url: str) -> dict[str, Any] | None:
    """Complete ChatGPT session directly from create_account callback/openai URL."""
    if self.session is None:
        return None
    callback_url = self._extract_callback_url(continue_url)
    if not callback_url:
        return None
    parsed = urllib.parse.urlparse(callback_url)
    if parsed.netloc != "chatgpt.com" or not parsed.path.startswith("/api/auth/callback/openai"):
        return None
    try:
        self._log("create-account callback session: attempting direct callback session exchange")
        response = self.session.get(
            callback_url,
            allow_redirects=True,
            timeout=15,
            headers={
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "referer": "https://chatgpt.com/",
            },
        )
        self._log(f"create-account callback session: callback status={response.status_code}")
        cookie_names: list[str] = []
        try:
            jar = getattr(self.session.cookies, "jar", None)
            if jar:
                cookie_names = [str(getattr(cookie, "name", "")) for cookie in list(jar)]
        except Exception:
            cookie_names = []
        self._log(f"callback session: cookies after callback: {cookie_names[:20]}")
    except Exception as exc:
        self._log(f"create-account callback session: callback request failed: {exc}")
        return None
    try:
        session_url = "https://chatgpt.com/api/auth/session"
        session_resp = self.session.get(
            session_url,
            headers={
                "accept": "application/json",
                "referer": "https://chatgpt.com/",
            },
            timeout=15,
        )
        self._log(f"create-account callback session: session status={session_resp.status_code}")
        if session_resp.status_code != 200:
            return None
        try:
            session_data = session_resp.json()
        except Exception:
            session_data = {}
        access_token = str(session_data.get("accessToken") or "").strip()
        if not access_token:
            self._log(
                "create-account callback session: accessToken missing, "
                f"keys={list(session_data.keys())}"
            )
            return None
        token_info = self._parse_session_jwt(access_token, session_data)
        token_info["source"] = "create_account_callback_session"
        self._log("create-account callback session: ✅ token acquired from callback/session path")
        return token_info
    except Exception as exc:
        self._log(f"create-account callback session: session extraction failed: {exc}")
        return None

def _try_direct_session_token(self) -> dict[str, Any] | None:
    """Try to extract accessToken by leveraging the existing auth session.

    After create_account, even though add-phone is returned, the user
    account exists and auth cookies may allow completing the OAuth flow
    without triggering a fresh login (which causes add-phone gate).

    Strategy:
      1. Log cookie diagnostics for debugging
      2. Try authorize flow using existing session to get code
      3. Exchange code at chatgpt.com callback
      4. GET chatgpt.com/api/auth/session for accessToken
    """
    if self.session is None:
        return None
    try:
        self._log("solution D: attempting direct session token extraction")

        # ── Step 0: cookie diagnostics ──
        try:
            cookie_jar = self.session.cookies
            # curl_cffi uses a dict-like cookie jar
            cookie_items = []
            try:
                # Try standard jar iteration
                for cookie in cookie_jar.jar:
                    cookie_items.append(f"{getattr(cookie, 'domain', '?')}:{getattr(cookie, 'name', '?')}")
            except Exception:
                try:
                    # Fallback: cookie jar as dict
                    for name, value in cookie_jar.items():
                        cookie_items.append(f"{name}={str(value)[:20]}")
                except Exception:
                    cookie_items.append(f"jar_type={type(cookie_jar).__name__}")
            self._log(f"solution D: cookies ({len(cookie_items)}): {cookie_items[:15]}")
        except Exception as exc:
            self._log(f"solution D: cookie diagnostics error: {exc}")

         # Re-use the already-built auth_url from oauth_start but remove
        # prompt=login to prevent re-authentication trigger.
        if self.oauth_start:
            try:
                parsed_auth = urllib.parse.urlparse(self.oauth_start.auth_url)
                auth_params = dict(urllib.parse.parse_qsl(parsed_auth.query))
                # Remove prompt=login to skip forced re-auth
                auth_params.pop("prompt", None)
                authorize_url = f"{parsed_auth.scheme}://{parsed_auth.netloc}{parsed_auth.path}?{urllib.parse.urlencode(auth_params)}"
                self._log("solution D: attempting authorize with existing session")
                auth_resp = self.session.get(
                    authorize_url,
                    allow_redirects=False,
                    timeout=15,
                )
                self._log(
                    f"solution D: authorize status={auth_resp.status_code}, "
                    f"location={str(auth_resp.headers.get('Location', ''))[:120]}"
                )

                # Follow redirect chain to find callback URL with code=
                callback_url = None
                current_url = str(auth_resp.headers.get("Location", "")).strip()
                if auth_resp.status_code in {301, 302, 303, 307, 308} and current_url:
                    for hop in range(8):
                        self._log(f"solution D: redirect hop {hop + 1}: {current_url[:120]}")
                        if "code=" in current_url and "state=" in current_url:
                            callback_url = current_url
                            self._log("solution D: ✅ callback URL extracted from authorize redirect")
                            break
                        try:
                            hop_resp = self.session.get(
                                current_url,
                                allow_redirects=False,
                                timeout=15,
                            )
                            next_loc = str(hop_resp.headers.get("Location", "")).strip()
                            if hop_resp.status_code not in {301, 302, 303, 307, 308} or not next_loc:
                                self._log(
                                    f"solution D: redirect chain ended at hop {hop + 1} "
                                    f"status={hop_resp.status_code}"
                                )
                                # Check if the final URL itself contains code=
                                final_url = str(hop_resp.url or current_url)
                                if "code=" in final_url and "state=" in final_url:
                                    callback_url = final_url
                                    self._log("solution D: ✅ callback URL found in final redirect URL")
                                break
                            current_url = urllib.parse.urljoin(current_url, next_loc)
                        except Exception as exc:
                            self._log(f"solution D: redirect hop {hop + 1} error: {exc}")
                            break
                elif auth_resp.status_code == 200:
                    # Might be a page that needs interaction (e.g., consent)
                    body_preview = str(auth_resp.text or "")[:200]
                    self._log(f"solution D: authorize returned 200, body={body_preview}")

                # If we got a callback URL, exchange it
                if callback_url:
                    self._log("solution D: exchanging callback code via oauth handler")
                    token_info = self._handle_oauth_callback(callback_url)
                    if token_info:
                        self._log("solution D: ✅ token obtained via authorize → callback exchange")
                        token_info["source"] = "direct_authorize"
                        return token_info
                    else:
                        self._log("solution D: callback exchange failed")

            except Exception as exc:
                self._log(f"solution D: authorize flow error: {exc}")

        # ── Step 2: try chatgpt.com/api/auth/session directly ──
        try:
            session_url = "https://chatgpt.com/api/auth/session"
            resp = self.session.get(
                session_url,
                headers={
                    "accept": "application/json",
                    "referer": "https://chatgpt.com/",
                },
                timeout=15,
            )
            self._log(f"solution D: session endpoint status={resp.status_code}")
            if resp.status_code == 200:
                try:
                    session_data = resp.json()
                except Exception:
                    session_data = {}
                body_summary = str(resp.text or "")[:200]
                self._log(f"solution D: session body summary: {body_summary}")

                access_token = str(session_data.get("accessToken") or "").strip()
                if access_token:
                    self._log(f"solution D: accessToken obtained (len={len(access_token)})")
                    token_info = self._parse_session_jwt(access_token, session_data)
                    return token_info
                else:
                    self._log(
                        f"solution D: no accessToken in session response, "
                        f"keys={list(session_data.keys())}"
                    )
        except Exception as exc:
            self._log(f"solution D: session endpoint error: {exc}")

        self._log("solution D: all extraction attempts failed")
        return None

    except Exception as exc:
        self._log(f"solution D: direct session token extraction failed: {exc}")
        return None


def _parse_session_jwt(self, access_token: str, session_data: dict[str, Any]) -> dict[str, Any]:
    """Parse accessToken JWT and build token_info dict."""
    token_info: dict[str, Any] = {
        "access_token": access_token,
        "source": "direct_session",
    }
    for key in ("user", "expires"):
        if key in session_data:
            token_info[key] = session_data[key]
    try:
        parts = access_token.split(".")
        if len(parts) >= 2:
            payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            if isinstance(payload, dict):
                for jwt_key, info_key in [
                    ("sub", "account_id"),
                    ("email", "email"),
                    ("exp", "expired"),
                ]:
                    if jwt_key in payload:
                        token_info[info_key] = str(payload[jwt_key])
                scope = payload.get("scope") or payload.get("scp")
                if scope:
                    token_info["scope"] = scope if isinstance(scope, str) else " ".join(scope)
                self._log(
                    f"solution D: JWT decoded - account_id={token_info.get('account_id', 'N/A')}, "
                    f"email={token_info.get('email', 'N/A')}, "
                    f"scope={str(token_info.get('scope', 'N/A'))[:80]}"
                )
    except Exception as exc:
        self._log(f"solution D: JWT decode warning: {exc}")
    self._log("solution D: ✅ direct session token extraction succeeded")
    return token_info


def _follow_redirects(self, start_url: str) -> str | None:
    if self.session is None:
        return None
    current_url = start_url
    try:
        for index in range(12):
            self._log(f"follow redirect {index + 1}: {current_url[:120]}")
            response = self.session.get(
                current_url,
                allow_redirects=False,
                timeout=15,
            )
            location = str(response.headers.get("Location") or "").strip()
            if response.status_code not in {301, 302, 303, 307, 308}:
                self._log(f"redirect chain ended with status {response.status_code}")
                break
            if not location:
                self._log("redirect location missing")
                break
            next_url = urllib.parse.urljoin(current_url, location)
            if "code=" in next_url and "state=" in next_url:
                self._log("oauth callback url reached")
                return next_url
            current_url = next_url
        self._log("callback url not reached in redirect chain")
        return None
    except Exception as exc:
        self._log(f"follow_redirects failed: {exc}")
        return None

def _handle_oauth_callback(self, callback_url: str) -> dict[str, Any] | None:
    if not self.oauth_start:
        return None
    try:
        self._log("handling oauth callback")
        token_info = self.oauth_manager.handle_callback(
            callback_url=callback_url,
            expected_state=self.oauth_start.state,
            code_verifier=self.oauth_start.code_verifier,
        )
        self._log("oauth callback exchanged successfully")
        return token_info
    except Exception as exc:
        self._log(f"handle_oauth_callback failed: {exc}")
        return None

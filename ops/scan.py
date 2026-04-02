"""Low-frequency token scan for local pool files."""

from __future__ import annotations

import argparse
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from curl_cffi import requests

from .common import DEFAULT_POOL_DIR

DEFAULT_TIMEOUT = 15
DEFAULT_WORKERS = 5
DEFAULT_TRANSPORT_MAX_ATTEMPTS = 3
VALIDATE_URL = "https://chatgpt.com/backend-api/wham/usage"
RESPONSES_VALIDATE_URL = "https://chatgpt.com/backend-api/codex/responses"
VALIDATE_USER_AGENT = "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal"
RESPONSES_VALIDATE_USER_AGENT = "Codex Desktop/0.115.0-alpha.27"
THREAD_LOCAL = threading.local()
PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "logs"
RESPONSES_PROBE_PAYLOAD = {
    "model": "gpt-5.4",
    "instructions": "Return exactly OK.",
    "input": [{"role": "user", "content": "ping"}],
    "stream": True,
    "store": False,
    "text": {"verbosity": "low"},
}


@dataclass(frozen=True)
class ScanResult:
    file: str
    category: str
    status_code: int | None
    detail: str


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def compact_text(value: str, limit: int = 240) -> str:
    return " ".join(str(value or "").split())[:limit]


def get_session() -> requests.Session:
    session = getattr(THREAD_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        THREAD_LOCAL.session = session
    return session


def reset_session() -> None:
    session = getattr(THREAD_LOCAL, "session", None)
    if session is None:
        return
    try:
        session.close()
    except Exception:
        pass
    THREAD_LOCAL.session = None


def is_transient_transport_error(exc: Exception) -> bool:
    message = str(exc or "").lower()
    markers = (
        "connection closed abruptly",
        "connection timed out",
        "connection reset",
        "connection refused",
        "tls connect error",
        "recv failure",
        "send failure",
        "http/2 stream",
        "operation timed out",
        "unexpected eof",
        "tls handshake timeout",
        "eof",
        "curl: (7)",
        "curl: (28)",
        "curl: (35)",
        "curl: (52)",
        "curl: (55)",
        "curl: (56)",
        "curl: (16)",
        "nghttp2",
    )
    return any(marker in message for marker in markers)


def iter_token_files(token_dir: Path, limit: int | None = None) -> list[Path]:
    files = sorted(path for path in token_dir.glob("*.json") if path.is_file())
    if limit is not None and limit >= 0:
        return files[:limit]
    return files


def _load_token_payload(path: Path) -> tuple[dict[str, object] | None, ScanResult | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        return None, ScanResult(file=path.name, category="missing", status_code=None, detail=f"missing_file: {exc}")
    except Exception as exc:
        return None, ScanResult(file=path.name, category="suspicious", status_code=None, detail=f"invalid_json: {exc}")

    if not isinstance(payload, dict):
        return None, ScanResult(file=path.name, category="suspicious", status_code=None, detail="invalid_json: token record must be object")
    return payload, None


def _extract_credentials(path: Path, payload: dict[str, object]) -> tuple[str, str] | ScanResult:
    access_token = str(payload.get("access_token") or "").strip()
    account_id = str(payload.get("account_id") or "").strip()
    if not access_token or not account_id:
        return ScanResult(
            file=path.name,
            category="suspicious",
            status_code=None,
            detail="missing access_token or account_id",
        )
    return access_token, account_id


def _request_with_retry(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    json_body: object | None,
    proxy: str | None,
    timeout: int,
) -> ScanResult | object:
    proxies = {"http": proxy, "https": proxy} if proxy else None
    last_exc: Exception | None = None
    response = None
    for attempt in range(1, DEFAULT_TRANSPORT_MAX_ATTEMPTS + 1):
        try:
            request_fn = getattr(get_session(), method.lower())
            response = request_fn(
                url,
                headers=headers,
                json=json_body,
                proxies=proxies,
                impersonate="chrome",
                timeout=timeout,
            )
            break
        except Exception as exc:
            last_exc = exc
            if not is_transient_transport_error(exc):
                return ScanResult(file="", category="suspicious", status_code=None, detail=f"request_error: {exc}")
            reset_session()
            if attempt >= DEFAULT_TRANSPORT_MAX_ATTEMPTS:
                return ScanResult(file="", category="transport_error", status_code=None, detail=f"transport_error: {exc}")
    if response is None:
        return ScanResult(file="", category="transport_error", status_code=None, detail=f"transport_error: {last_exc or 'request failed'}")
    return response


def _probe_usage_path(path: Path, access_token: str, account_id: str, proxy: str | None, timeout: int) -> ScanResult:
    response = _request_with_retry(
        "GET",
        VALIDATE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": VALIDATE_USER_AGENT,
            "Chatgpt-Account-Id": account_id,
        },
        json_body=None,
        proxy=proxy,
        timeout=timeout,
    )
    if isinstance(response, ScanResult):
        return ScanResult(file=path.name, category=response.category, status_code=response.status_code, detail=response.detail)

    detail = compact_text(response.text)
    if response.status_code == 200:
        return ScanResult(file=path.name, category="normal", status_code=200, detail=detail)
    if response.status_code == 401:
        return ScanResult(file=path.name, category="invalid", status_code=401, detail=detail)
    if response.status_code == 429:
        return ScanResult(file=path.name, category="rate_limited", status_code=429, detail=detail)
    if int(response.status_code or 0) >= 500:
        return ScanResult(file=path.name, category="service_error", status_code=int(response.status_code), detail=detail)
    return ScanResult(file=path.name, category="suspicious", status_code=response.status_code, detail=detail)


def _probe_responses_path(path: Path, access_token: str, account_id: str, proxy: str | None, timeout: int) -> ScanResult:
    response = _request_with_retry(
        "POST",
        RESPONSES_VALIDATE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": RESPONSES_VALIDATE_USER_AGENT,
            "Chatgpt-Account-Id": account_id,
            "Accept": "text/event-stream",
        },
        json_body=RESPONSES_PROBE_PAYLOAD,
        proxy=proxy,
        timeout=max(timeout, 20),
    )
    if isinstance(response, ScanResult):
        return ScanResult(file=path.name, category=response.category, status_code=response.status_code, detail=response.detail)

    detail = compact_text(response.text, limit=320)
    if response.status_code == 200:
        if "response.failed" in detail or '"status":"failed"' in detail:
            return ScanResult(file=path.name, category="service_error", status_code=200, detail=detail)
        return ScanResult(file=path.name, category="normal", status_code=200, detail="responses_ok")
    if response.status_code == 401:
        return ScanResult(file=path.name, category="invalid", status_code=401, detail=detail)
    if response.status_code == 429:
        return ScanResult(file=path.name, category="rate_limited", status_code=429, detail=detail)
    if int(response.status_code or 0) >= 500:
        return ScanResult(file=path.name, category="service_error", status_code=int(response.status_code), detail=detail)
    return ScanResult(file=path.name, category="suspicious", status_code=response.status_code, detail=detail)


def classify_token_file(
    path: Path,
    proxy: str | None,
    timeout: int,
    *,
    require_response_path: bool = False,
) -> ScanResult:
    payload, load_error = _load_token_payload(path)
    if load_error is not None:
        return load_error
    assert payload is not None

    credentials = _extract_credentials(path, payload)
    if isinstance(credentials, ScanResult):
        return credentials
    access_token, account_id = credentials

    usage_result = _probe_usage_path(path, access_token, account_id, proxy, timeout)
    if not require_response_path or usage_result.category != "normal":
        return usage_result

    response_result = _probe_responses_path(path, access_token, account_id, proxy, timeout)
    if response_result.category == "normal":
        return ScanResult(file=path.name, category="normal", status_code=200, detail="usage_ok | responses_ok")
    return response_result


def scan_once(
    token_dir: Path,
    proxy: str | None,
    timeout: int,
    workers: int,
    output_dir: Path,
    limit: int | None = None,
) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    files = iter_token_files(token_dir, limit=limit)
    results: list[ScanResult] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        future_map = {executor.submit(classify_token_file, path, proxy, timeout): path for path in files}
        for future in as_completed(future_map):
            results.append(future.result())

    results.sort(key=lambda item: item.file)
    summary = {
        "total": len(results),
        "normal": sum(1 for item in results if item.category == "normal"),
        "invalid": sum(1 for item in results if item.category == "invalid"),
        "rate_limited": sum(1 for item in results if item.category == "rate_limited"),
        "suspicious": sum(1 for item in results if item.category == "suspicious"),
        "service_error": sum(1 for item in results if item.category == "service_error"),
        "transport_error": sum(1 for item in results if item.category == "transport_error"),
        "missing": sum(1 for item in results if item.category == "missing"),
    }
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = output_dir / f"scan_report_{timestamp}.json"
    payload = {
        "generated_at": now_iso(),
        "token_dir": str(token_dir),
        "proxy": proxy,
        "timeout_seconds": timeout,
        "workers": workers,
        "limit": limit,
        "summary": summary,
        "results": [asdict(item) for item in results],
    }
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "summary": summary,
        "report_path": str(report_path),
        "results": payload["results"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Low-frequency scan for local zhuce6 token files")
    parser.add_argument("--token-dir", default=str(DEFAULT_POOL_DIR), help="Token directory, default zhuce6 pool")
    parser.add_argument("--proxy", default=None, help="Optional proxy URL")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Per-request timeout seconds")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="Concurrent workers")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Report output directory")
    parser.add_argument("--limit", type=int, default=None, help="Optional cap for scanned files")
    args = parser.parse_args()

    token_dir = Path(args.token_dir).expanduser().resolve()
    if not token_dir.is_dir():
        raise SystemExit(f"token directory does not exist: {token_dir}")

    summary = scan_once(
        token_dir=token_dir,
        proxy=str(args.proxy or "").strip() or None,
        timeout=max(1, int(args.timeout)),
        workers=max(1, int(args.workers)),
        output_dir=Path(args.output_dir).expanduser().resolve(),
        limit=args.limit,
    )
    stats = summary["summary"]
    print(f"[scan] dir={token_dir}")
    print(f"[scan] total={stats['total']} normal={stats['normal']} invalid={stats['invalid']} suspicious={stats['suspicious']}")
    print(f"[scan] report={summary['report_path']}")


if __name__ == "__main__":
    main()

"""Validate backend auth files and optionally remove confirmed 401 entries.

This validator relies on CPA management API to classify auth files when CPA backend is active.
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .common import CpaClient, DEFAULT_MANAGEMENT_BASE_URL, DEFAULT_POOL_DIR, get_management_key, now

CPA_INVALID_KEYWORDS = ("unauthorized", "invalidated")


@dataclass(frozen=True)
class ValidateEntry:
    name: str
    status_code: int
    action: str
    detail: str = ""
    auth_index: str = ""
    account_id: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _compact_text(value: str, limit: int = 200) -> str:
    return " ".join(str(value or "").split())[:limit]


def _delete_cpa_file(name: str, client: object | None = None) -> bool:
    if client is None or not hasattr(client, "delete_auth_file"):
        return False
    return bool(getattr(client, "delete_auth_file")(name))


def _delete_pool_backup(pool_dir: Path, name: str) -> None:
    (pool_dir / name).unlink(missing_ok=True)


def _iter_auth_files(snapshot_dir: Path, limit: int | None = None) -> list[Path]:
    files = sorted(
        path
        for path in snapshot_dir.glob("*.json")
        if path.is_file() and "@" in path.name
    )
    if limit is not None and limit >= 0:
        return files[:limit]
    return files


def _extract_account_id(data: dict[str, object]) -> str:
    return str(data.get("account_id") or "").strip()


def _fetch_management_json(
    management_base_url: str,
    suffix: str,
    management_key: str | None = None,
) -> tuple[bool, dict[str, object] | None]:
    key = str(management_key or "").strip() or get_management_key()
    if not key:
        return False, None

    request = Request(
        f"{management_base_url.rstrip('/')}/{suffix.lstrip('/')}",
        headers={"Authorization": f"Bearer {key}"},
    )
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError):
        return False, None

    return isinstance(payload, dict), payload if isinstance(payload, dict) else None


def _parse_management_status_message(status_message: str) -> tuple[int, str]:
    raw = str(status_message or "").strip()
    if not raw:
        return 200, "active"

    lowered = raw.lower()
    if any(keyword in lowered for keyword in CPA_INVALID_KEYWORDS):
        return 401, "unauthorized"

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0, _compact_text(raw)

    if not isinstance(payload, dict):
        return 0, _compact_text(raw)

    err = payload.get("error")
    if isinstance(err, dict):
        err_type = str(err.get("type") or "").strip().lower()
        err_message = str(err.get("message") or "").strip()
        if err_type:
            if err_type in {"unauthorized", "invalidated"}:
                return 401, err_message or err_type
            if err_type in {"usage_limit_reached", "rate_limit_exceeded"}:
                return 429, err_message or err_type
            return 0, err_message or err_type

    return 0, _compact_text(raw)


def _fetch_management_auth_files(
    management_base_url: str,
    management_key: str | None = None,
) -> tuple[bool, dict[str, dict[str, object]]]:
    ok, payload = _fetch_management_json(management_base_url, "auth-files", management_key)
    if not ok or payload is None:
        return False, {}

    files = payload.get("files")
    if not isinstance(files, list):
        return False, {}

    result: dict[str, dict[str, object]] = {}
    for item in files:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        result[name] = item
    return True, result


def _fetch_used_auth_indexes(
    management_base_url: str,
    management_key: str | None = None,
) -> tuple[bool, set[str]]:
    ok, payload = _fetch_management_json(management_base_url, "usage", management_key)
    if not ok or payload is None:
        return False, set()

    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return True, set()

    auth_indexes: set[str] = set()
    apis = usage.get("apis")
    if not isinstance(apis, dict):
        return True, auth_indexes

    for api_data in apis.values():
        if not isinstance(api_data, dict):
            continue
        models = api_data.get("models")
        if not isinstance(models, dict):
            continue
        for model_data in models.values():
            if not isinstance(model_data, dict):
                continue
            details = model_data.get("details")
            if not isinstance(details, list):
                continue
            for detail in details:
                if not isinstance(detail, dict):
                    continue
                auth_index = str(detail.get("auth_index") or "").strip()
                if auth_index:
                    auth_indexes.add(auth_index)
    return True, auth_indexes


def _select_auth_files(
    auth_files: list[Path],
    *,
    scope: str,
    management_base_url: str,
    management_key: str | None = None,
) -> tuple[list[Path], bool, str | None]:
    if scope == "all":
        return auth_files, False, None

    auth_ok, auth_meta = _fetch_management_auth_files(management_base_url, management_key)
    usage_ok, used_auth_indexes = _fetch_used_auth_indexes(management_base_url, management_key)
    if not auth_ok or not usage_ok:
        return [], True, "management_data_unavailable"
    if not used_auth_indexes:
        return [], False, "no_active_auth_indexes"

    selected = [
        path
        for path in auth_files
        if str(auth_meta.get(path.name, {}).get("auth_index") or "").strip() in used_auth_indexes
    ]
    return selected, False, None


def _validate_file(path: Path, auth_meta: dict[str, object] | None) -> ValidateEntry:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return ValidateEntry(name=path.name, status_code=0, action="error", detail=f"json decode failed: {exc}")

    account_id = _extract_account_id(data)
    if not isinstance(auth_meta, dict):
        return ValidateEntry(
            name=path.name,
            status_code=0,
            action="skip",
            detail="missing management metadata",
            account_id=account_id,
        )

    auth_index = str(auth_meta.get("auth_index") or "").strip()
    status_message = str(auth_meta.get("status_message") or "").strip()
    status = str(auth_meta.get("status") or "").strip().lower()
    status_code, parsed_detail = _parse_management_status_message(status_message)

    if status_code == 401:
        return ValidateEntry(
            name=path.name,
            status_code=401,
            action="delete",
            detail="unauthorized by CPA management",
            auth_index=auth_index,
            account_id=account_id,
        )

    detail = parsed_detail
    if status and status != "active":
        detail = f"{status} | {parsed_detail}"

    return ValidateEntry(
        name=path.name,
        status_code=status_code,
        action="keep",
        detail=detail,
        auth_index=auth_index,
        account_id=account_id,
    )


def validate_once(
    proxy: str | None = None,
    dry_run: bool = False,
    max_workers: int = 8,
    limit: int | None = None,
    pool_dir: Path = DEFAULT_POOL_DIR,
    *,
    client: object | None = None,
    scope: str = "all",
    management_base_url: str = DEFAULT_MANAGEMENT_BASE_URL,
    management_key: str | None = None,
) -> dict[str, object]:
    del proxy
    pool_dir = Path(pool_dir).expanduser().resolve()
    pool_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "scope": scope,
        "checked": 0,
        "selected": 0,
        "kept": 0,
        "invalid": 0,
        "deleted": 0,
        "skipped": 0,
        "errors": 0,
        "dry_run": dry_run,
        "results": [],
        "validation_limited": False,
        "selection_reason": None,
    }

    backend_client = client or CpaClient(management_base_url, management_key=management_key)
    if not getattr(backend_client, "health_check")():
        summary["validation_limited"] = True
        summary["selection_reason"] = "cpa_unavailable"
        return summary

    snapshot_dir = Path(tempfile.mkdtemp(prefix="zhuce6_validate_", dir="/tmp"))
    try:
        for entry in getattr(backend_client, "list_auth_files")():
            name = str(entry.get("name") or "").strip()
            if not name or "@" not in name or not name.endswith(".json"):
                continue
            payload = getattr(backend_client, "get_auth_file")(name)
            if not isinstance(payload, dict):
                continue
            (snapshot_dir / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        auth_files = _iter_auth_files(snapshot_dir, limit=limit)
        selected_files, limited, selection_reason = _select_auth_files(
            auth_files,
            scope=scope,
            management_base_url=management_base_url,
            management_key=management_key,
        )
        auth_ok, auth_meta = (True, {}) if scope == "all" else _fetch_management_auth_files(management_base_url, management_key)
        summary["selected"] = len(selected_files)
        summary["validation_limited"] = limited
        summary["selection_reason"] = selection_reason
        if limited:
            return summary
        if not auth_ok:
            summary["validation_limited"] = True
            summary["selection_reason"] = "management_data_unavailable"
            return summary
        if not selected_files:
            return summary

        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
            future_map = {
                executor.submit(
                    _validate_file,
                    path,
                    {} if scope == "all" else auth_meta.get(path.name),
                ): path
                for path in selected_files
            }
            for future in as_completed(future_map):
                entry = future.result()
                summary["checked"] = int(summary["checked"]) + 1
                cast_results = summary["results"]
                assert isinstance(cast_results, list)
                cast_results.append(entry.to_dict())
                if entry.action == "keep":
                    summary["kept"] = int(summary["kept"]) + 1
                    print(f"[{now()}] [validate] ✅ {entry.name} keep | {entry.status_code}")
                    continue
                if entry.action == "skip":
                    summary["skipped"] = int(summary["skipped"]) + 1
                    print(f"[{now()}] [validate] ⏭️ {entry.name} skip | {entry.detail}")
                    continue
                if entry.action == "error":
                    summary["errors"] = int(summary["errors"]) + 1
                    print(f"[{now()}] [validate] ⚠️ {entry.name} error | {entry.detail}")
                    continue
                if entry.action == "delete":
                    summary["invalid"] = int(summary["invalid"]) + 1
                    if dry_run:
                        print(f"[{now()}] [validate] 🧪 {entry.name} would delete | 401")
                        continue
                    deleted = _delete_cpa_file(entry.name, backend_client)
                    if deleted:
                        _delete_pool_backup(pool_dir, entry.name)
                        summary["deleted"] = int(summary["deleted"]) + 1
                        print(f"[{now()}] [validate] ❌ {entry.name} deleted")
                    else:
                        summary["errors"] = int(summary["errors"]) + 1
                        print(f"[{now()}] [validate] ⚠️ {entry.name} delete failed")
    finally:
        shutil.rmtree(snapshot_dir, ignore_errors=True)

    return summary


def print_validate_summary(summary: dict[str, object]) -> None:
    selection_reason = summary.get("selection_reason") or "-"
    print(
        f"[{now()}] [validate] summary"
        f" | scope={summary['scope']}"
        f" | selected={summary['selected']}"
        f" | checked={summary['checked']}"
        f" | kept={summary['kept']}"
        f" | invalid={summary['invalid']}"
        f" | deleted={summary['deleted']}"
        f" | skipped={summary['skipped']}"
        f" | errors={summary['errors']}"
        f" | dry_run={summary['dry_run']}"
        f" | validation_limited={summary['validation_limited']}"
        f" | selection_reason={selection_reason}"
    )


def main() -> None:
    from core.settings import AppSettings

    env_settings = AppSettings.from_env()
    parser = argparse.ArgumentParser(description="Validate zhuce6 backend tokens and classify 401 files")
    parser.add_argument("--once", action="store_true", help="Compatibility flag. Validation runs once either way.")
    parser.add_argument("--dry-run", action="store_true", help="Do not delete files, only report them.")
    parser.add_argument("--proxy", default=None, help="Optional proxy URL")
    parser.add_argument("--management-base-url", default=env_settings.cpa_management_base_url or DEFAULT_MANAGEMENT_BASE_URL, help="CPA management base url")
    parser.add_argument("--management-key", default=env_settings.cpa_management_key, help="可选 CPA management key")
    parser.add_argument("--scope", choices=("all", "used"), default="all", help="all=full validate, used=fast validate using CPA management usage")
    parser.add_argument("--max-workers", type=int, default=8, help="Concurrent validation workers")
    parser.add_argument("--limit", type=int, default=None, help="Optional cap for scanned auth files")
    parser.add_argument("--pool-dir", default=str(env_settings.pool_dir or DEFAULT_POOL_DIR), help="本地 pool 目录")
    args = parser.parse_args()

    del args.once
    summary = validate_once(
        proxy=str(args.proxy or "").strip() or None,
        dry_run=args.dry_run,
        max_workers=args.max_workers,
        limit=args.limit,
        pool_dir=Path(args.pool_dir).expanduser().resolve(),
        scope=args.scope,
        management_base_url=str(args.management_base_url or "").strip() or DEFAULT_MANAGEMENT_BASE_URL,
        management_key=str(args.management_key or "").strip() or None,
    )
    print_validate_summary(summary)


if __name__ == "__main__":
    main()

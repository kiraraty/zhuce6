"""Fixed cohort survival tracking for newly created accounts."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any

from platforms.chatgpt.pool import load_token_record

from .scan import ScanResult, classify_token_file


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _parse_iso(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except Exception:
        return None


def _duration_seconds(started_at: str, ended_at: str) -> int | None:
    start_dt = _parse_iso(started_at)
    end_dt = _parse_iso(ended_at)
    if start_dt is None or end_dt is None:
        return None
    return max(0, int((end_dt - start_dt).total_seconds()))


def _compact_text(value: str, limit: int = 240) -> str:
    return " ".join(str(value or "").split())[:limit]


def _state_template(
    *,
    pool_dir: Path,
    cohort_size: int,
    proxy: str | None,
    timeout_seconds: int,
    seed_source: str = "latest_generated_pool_files",
) -> dict[str, Any]:
    return {
        "updated_at": "",
        "seeded_at": "",
        "seed_source": seed_source,
        "pool_dir": str(pool_dir),
        "cohort_size": max(1, int(cohort_size)),
        "proxy": str(proxy or "").strip() or None,
        "timeout_seconds": max(5, int(timeout_seconds)),
        "members": [],
        "summary": {
            "tracked": 0,
            "alive": 0,
            "invalid": 0,
            "missing": 0,
            "transport_error": 0,
            "suspicious": 0,
            "never_probed": 0,
            "first_invalid_count": 0,
        },
        "changes": [],
    }


def load_account_survival_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _persist_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def _seed_member(path: Path) -> dict[str, Any] | None:
    try:
        payload = load_token_record(path)
    except Exception:
        return None
    email = str(payload.get("email") or "").strip()
    access_token = str(payload.get("access_token") or "").strip()
    account_id = str(payload.get("account_id") or "").strip()
    if not email or not access_token or not account_id:
        return None
    created_at = str(payload.get("created_at") or "").strip()
    if not created_at:
        created_at = datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    selected_at = now_iso()
    return {
        "email": email,
        "file_name": path.name,
        "path": str(path),
        "created_at": created_at,
        "selected_at": selected_at,
        "first_probe_at": "",
        "last_probe_at": "",
        "probe_count": 0,
        "last_probe_status_code": None,
        "last_probe_category": "",
        "last_probe_detail": "",
        "transport_error_count": 0,
        "suspicious_count": 0,
        "missing_at": "",
        "first_invalid_at": "",
        "survival_seconds": None,
        "state": "tracking",
    }


def _seed_members(pool_dir: Path, cohort_size: int) -> list[dict[str, Any]]:
    candidates: list[tuple[float, dict[str, Any]]] = []
    for path in pool_dir.glob("*.json"):
        if not path.is_file():
            continue
        member = _seed_member(path)
        if member is None:
            continue
        created_at = _parse_iso(str(member.get("created_at") or ""))
        sort_ts = created_at.timestamp() if created_at is not None else path.stat().st_mtime
        candidates.append((sort_ts, member))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [member for _ts, member in candidates[: max(1, int(cohort_size))]]


def _update_member(member: dict[str, Any], result: ScanResult, probed_at: str) -> dict[str, Any]:
    previous_category = str(member.get("last_probe_category") or "").strip()
    member["last_probe_at"] = probed_at
    if not str(member.get("first_probe_at") or "").strip():
        member["first_probe_at"] = probed_at
    member["probe_count"] = int(member.get("probe_count") or 0) + 1
    member["last_probe_status_code"] = result.status_code
    member["last_probe_category"] = result.category
    member["last_probe_detail"] = _compact_text(result.detail or "")

    if result.category == "transport_error":
        member["transport_error_count"] = int(member.get("transport_error_count") or 0) + 1
    elif result.category == "suspicious":
        member["suspicious_count"] = int(member.get("suspicious_count") or 0) + 1
    elif result.category == "missing" and not str(member.get("missing_at") or "").strip():
        member["missing_at"] = probed_at

    if result.category == "invalid":
        if not str(member.get("first_invalid_at") or "").strip():
            member["first_invalid_at"] = probed_at
            survival_seconds = _duration_seconds(
                str(member.get("created_at") or "").strip() or str(member.get("first_probe_at") or "").strip(),
                probed_at,
            )
            member["survival_seconds"] = survival_seconds
        member["state"] = "invalid"
    elif result.category == "missing":
        member["state"] = "missing"
    else:
        member["state"] = "tracking"

    return {
        "email": str(member.get("email") or "").strip(),
        "from": previous_category or "never_probed",
        "to": result.category,
        "probed_at": probed_at,
        "survival_seconds": member.get("survival_seconds"),
        "detail": member["last_probe_detail"],
    }


def _build_summary(members: list[dict[str, Any]]) -> dict[str, int]:
    summary = {
        "tracked": len(members),
        "alive": 0,
        "invalid": 0,
        "missing": 0,
        "transport_error": 0,
        "suspicious": 0,
        "never_probed": 0,
        "first_invalid_count": 0,
    }
    for member in members:
        category = str(member.get("last_probe_category") or "").strip()
        if not category:
            summary["never_probed"] += 1
        elif category == "normal":
            summary["alive"] += 1
        elif category == "invalid":
            summary["invalid"] += 1
        elif category == "missing":
            summary["missing"] += 1
        elif category == "transport_error":
            summary["transport_error"] += 1
        else:
            summary["suspicious"] += 1
        if str(member.get("first_invalid_at") or "").strip():
            summary["first_invalid_count"] += 1
    return summary


def account_survival_once(
    *,
    pool_dir: Path,
    state_file: Path,
    cohort_size: int,
    proxy: str | None,
    timeout_seconds: int,
    reseed: bool = False,
) -> dict[str, Any]:
    state = load_account_survival_state(state_file)
    seeded = False
    reseeded = False

    if not state or reseed:
        state = _state_template(
            pool_dir=pool_dir,
            cohort_size=cohort_size,
            proxy=proxy,
            timeout_seconds=timeout_seconds,
        )
        state["members"] = _seed_members(pool_dir, int(state.get("cohort_size") or cohort_size))
        state["seeded_at"] = now_iso()
        seeded = True
        reseeded = reseed
    else:
        state.setdefault("pool_dir", str(pool_dir))
        state.setdefault("cohort_size", max(1, int(cohort_size)))
        state.setdefault("proxy", str(proxy or "").strip() or None)
        state.setdefault("timeout_seconds", max(5, int(timeout_seconds)))
        state.setdefault("members", [])
        state.setdefault("summary", {})
        state.setdefault("changes", [])
        state.setdefault("seed_source", "latest_generated_pool_files")

    if not isinstance(state.get("members"), list):
        state["members"] = []

    if not state["members"]:
        state["members"] = _seed_members(pool_dir, int(state.get("cohort_size") or cohort_size))
        state["seeded_at"] = now_iso()
        seeded = True

    changes: list[dict[str, Any]] = []
    for raw_member in state["members"]:
        if not isinstance(raw_member, dict):
            continue
        member = raw_member
        probed_at = now_iso()
        result = classify_token_file(
            Path(str(member.get("path") or "")),
            str(state.get("proxy") or "").strip() or None,
            max(5, int(state.get("timeout_seconds") or timeout_seconds)),
        )
        change = _update_member(member, result, probed_at)
        if change["from"] != change["to"]:
            changes.append(change)

    state["updated_at"] = now_iso()
    state["summary"] = _build_summary([member for member in state["members"] if isinstance(member, dict)])
    state["changes"] = changes
    state["seeded"] = seeded
    state["reseeded"] = reseeded
    state["state_file"] = str(state_file)
    _persist_state(state_file, state)
    return state


def print_account_survival_summary(result: dict[str, Any]) -> None:
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    tracked = int(summary.get("tracked") or 0)
    alive = int(summary.get("alive") or 0)
    invalid = int(summary.get("invalid") or 0)
    missing = int(summary.get("missing") or 0)
    transport_error = int(summary.get("transport_error") or 0)
    suspicious = int(summary.get("suspicious") or 0)
    state_file = str(result.get("state_file") or "")
    print(
        f"[survival] summary | tracked={tracked} | alive={alive} | invalid={invalid} "
        f"| missing={missing} | transport_error={transport_error} | suspicious={suspicious}"
    )
    if result.get("seeded"):
        members = result.get("members") if isinstance(result.get("members"), list) else []
        emails = ", ".join(
            str(item.get("email") or "").strip()
            for item in members
            if isinstance(item, dict) and str(item.get("email") or "").strip()
        )
        print(f"[survival] seeded fixed cohort | count={len(members)} | members={emails}")
    for change in result.get("changes") or []:
        if not isinstance(change, dict):
            continue
        survival_seconds = change.get("survival_seconds")
        survival_text = f" | survival={survival_seconds}s" if survival_seconds is not None else ""
        print(
            f"[survival] state change | {change.get('email') or '?'} | "
            f"{change.get('from') or 'never_probed'} -> {change.get('to') or '?'}{survival_text}"
        )
    if state_file:
        print(f"[survival] state={state_file}")

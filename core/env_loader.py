"""Environment bootstrap helpers for zhuce6 entrypoints."""

from __future__ import annotations

import os
from pathlib import Path


_BOOTSTRAPPED = False


def _resolve_project_root(project_root: Path | None = None) -> Path:
    if project_root is not None:
        return project_root.expanduser().resolve()
    raw = str(os.getenv("ZHUCE6_PROJECT_ROOT", "")).strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def bootstrap_env(project_root: Path | None = None, *, force: bool = False) -> tuple[Path, Path, Path]:
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED and not force:
        resolved_root = _resolve_project_root(project_root)
        config_dir = Path(
            str(os.getenv("ZHUCE6_CONFIG_DIR", resolved_root / "config")).strip() or str(resolved_root / "config")
        ).expanduser().resolve()
        env_file = Path(
            str(os.getenv("ZHUCE6_ENV_FILE", resolved_root / ".env")).strip() or str(resolved_root / ".env")
        ).expanduser().resolve()
        cfmail_env_file = Path(
            str(os.getenv("ZHUCE6_CFMAIL_ENV_FILE", config_dir / "cfmail_provision.env")).strip()
            or str(config_dir / "cfmail_provision.env")
        ).expanduser().resolve()
        return resolved_root, env_file, cfmail_env_file

    resolved_root = _resolve_project_root(project_root)
    os.environ.setdefault("ZHUCE6_PROJECT_ROOT", str(resolved_root))

    env_file = Path(
        str(os.getenv("ZHUCE6_ENV_FILE", resolved_root / ".env")).strip() or str(resolved_root / ".env")
    ).expanduser().resolve()
    load_env_file(env_file)

    config_dir = Path(
        str(os.getenv("ZHUCE6_CONFIG_DIR", resolved_root / "config")).strip() or str(resolved_root / "config")
    ).expanduser().resolve()
    cfmail_env_file = Path(
        str(os.getenv("ZHUCE6_CFMAIL_ENV_FILE", config_dir / "cfmail_provision.env")).strip()
        or str(config_dir / "cfmail_provision.env")
    ).expanduser().resolve()
    load_env_file(cfmail_env_file)

    _BOOTSTRAPPED = True
    return resolved_root, env_file, cfmail_env_file

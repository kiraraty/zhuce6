"""Plugin registry for zhuce6 platforms."""

from __future__ import annotations

import importlib
import pkgutil
from typing import Type

from .base_platform import BasePlatform

_registry: dict[str, Type[BasePlatform]] = {}


def register(cls: Type[BasePlatform]) -> Type[BasePlatform]:
    _registry[cls.name] = cls
    return cls


def load_all() -> None:
    import platforms

    for _, name, _ in pkgutil.iter_modules(platforms.__path__, platforms.__name__ + "."):
        try:
            importlib.import_module(f"{name}.plugin")
        except ModuleNotFoundError:
            continue


def get(name: str) -> Type[BasePlatform]:
    if name not in _registry:
        raise KeyError(f"Unknown platform: {name}. Registered: {list(_registry)}")
    return _registry[name]


def list_platforms() -> list[dict[str, str]]:
    return [
        {"name": cls.name, "display_name": cls.display_name, "version": cls.version}
        for cls in _registry.values()
    ]


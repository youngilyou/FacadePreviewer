"""YAML pipeline config loader (CLAUDE.local.md #33)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class Config:
    """Read-only dot-access wrapper around a nested dict loaded from YAML."""

    def __init__(self, data: dict):
        self._data = data

    def __getattr__(self, name: str) -> Any:
        try:
            value = self._data[name]
        except KeyError as exc:
            raise AttributeError(f"config has no key '{name}'") from exc
        if isinstance(value, dict):
            return Config(value)
        return value

    def __getitem__(self, name: str) -> Any:
        return getattr(self, name)

    def __contains__(self, name: str) -> bool:
        return name in self._data

    def __repr__(self) -> str:
        return f"Config({self._data!r})"

    def to_dict(self) -> dict:
        return self._data


def _deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: str | Path) -> Config:
    """A file may start with `extends: <other.yaml>` (relative to itself) and then list only the
    keys it changes -- e.g. config/pipeline.laptop.yaml on top of config/pipeline.yaml."""
    path = Path(path)
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    parent = data.pop("extends", None)
    if parent:
        data = _deep_merge(load_config(path.parent / parent).to_dict(), data)
    return Config(data)

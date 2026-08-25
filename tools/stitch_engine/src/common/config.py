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


def load_config(path: str | Path) -> Config:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return Config(data)

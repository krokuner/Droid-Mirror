"""A tiny JSON-backed store for UI preferences.

Keybinds live in profiles (see keymap.py); this is only for small "remember
this between launches" switches such as the USB/Wireless device filter. Kept
deliberately dumb: missing/corrupt files read as empty, writes are best-effort
so a read-only checkout never breaks startup.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import settings_path


class UiSettings:
    """Flat string/number values persisted to ``settings.json``."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or settings_path()
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Store ``key`` and persist, unless it already holds ``value``."""
        if self._data.get(key) == value:
            return
        self._data[key] = value
        self.save()

    def save(self) -> None:
        try:
            self.path.write_text(
                json.dumps(self._data, indent=2), encoding="utf-8"
            )
        except OSError:
            pass  # preferences are best-effort

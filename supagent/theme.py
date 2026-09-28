"""The look of Superset, for supagent's pages: whether dark mode exists (an admin can turn it
off with THEME_DARK = None, or set system themes in the UI) and the primary color. Which mode
a user sees is chosen in the browser, as Superset does it (static/supagent/theme.js)."""

from __future__ import annotations

import re
from typing import Any

HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def superset_theme() -> dict[str, Any]:
    """{"dark": dark mode available, "primary": light-mode primary color, "primary_dark": ...}"""
    try:
        from superset.views.base import get_theme_bootstrap_data

        theme = get_theme_bootstrap_data().get("theme") or {}
    except Exception:  # pylint: disable=broad-except   (Superset without themes: light only)
        return {"dark": False, "primary": None, "primary_dark": None}
    default, dark = theme.get("default") or {}, theme.get("dark") or {}
    light_tokens = default.get("token") or {}
    dark_tokens = {**light_tokens, **(dark.get("token") or {})}

    def color(tokens: dict[str, Any]) -> str | None:
        value = str(tokens.get("colorPrimary") or "")
        return value if HEX.match(value) else None

    return {"dark": bool(dark), "primary": color(light_tokens), "primary_dark": color(dark_tokens)}

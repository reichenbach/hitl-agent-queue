"""Shared normalization helpers for text shown at trust boundaries."""

from __future__ import annotations

import re
import unicodedata


_BIDI_CONTROLS = "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"


def sanitize_summary(
    summary: str,
    *,
    limit: int = 160,
    ellipsis: bool = False,
    escape_ampersand: bool = False,
) -> str:
    """Return a single-line, bounded summary safe for logs and menu labels."""
    summary = "".join(
        " " if unicodedata.category(char) == "Cc" or char in _BIDI_CONTROLS else char
        for char in str(summary)
    )
    summary = re.sub(r"\s+", " ", summary).strip()
    summary = re.sub(r"(?i)\b(APPROVE|SKIP)(?=\s+TKT-)", r"\1_", summary)
    if ellipsis and len(summary) > limit:
        summary = summary[: max(0, limit - 1)].rstrip() + "…"
    else:
        summary = summary[:limit]
    summary = summary or "(no summary)"
    return summary.replace("&", "&&") if escape_ampersand else summary

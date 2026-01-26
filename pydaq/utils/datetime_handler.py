"""Timestamp parsing helpers.

In field deployments, instruments often emit a limited set of timestamp formats.
This module provides a small best-effort parser so drivers can stay lean.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def parse_isoish(text: str) -> Optional[datetime]:
    """Parse common timestamp strings into a ``datetime``.

    Supported examples (best-effort):
    - ``2025-01-01 00:00:00``
    - ``2025-01-01T00:00:00``
    - ``2025-01-01T00:00:00Z``
    - ``2025-01-01 00:00:00Z``

    Returns:
        Parsed datetime, or ``None`` if parsing fails.
    """
    if not text:
        return None

    s = text.strip()
    try:
        if s.endswith("Z"):
            s2 = s[:-1].replace(" ", "T") + "+00:00"
            return datetime.fromisoformat(s2)

        if "T" in s and ("+" in s or s.count(":") >= 2):
            return datetime.fromisoformat(s)

        if " " in s and s.count(":") >= 2 and "-" in s:
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)

        if "T" in s and s.count(":") >= 2 and "-" in s:
            return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None

    return None

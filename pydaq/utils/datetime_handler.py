"""Timestamp parsing helpers.

In field deployments, instruments often emit a limited set of timestamp formats.
This module provides a small best-effort parser so drivers can stay lean.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def parse_isoish(text: str) -> Optional[datetime]:
    """Parse common instrument timestamp strings into UTC datetimes."""
    if not text:
        return None

    value = text.strip()

    try:
        if value.endswith("Z"):
            normalized = value[:-1].replace(" ", "T") + "+00:00"
            return datetime.fromisoformat(normalized)

        # ISO timestamps, including timestamps with offsets.
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            pass

        formats = (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%m/%d/%Y %I:%M:%S %p",  # AE33: 7/29/2026 6:06:00 AM
            "%m/%d/%Y %H:%M:%S",
        )

        for timestamp_format in formats:
            try:
                return datetime.strptime(value, timestamp_format).replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue

    except (TypeError, ValueError, OverflowError):
        return None

    return None
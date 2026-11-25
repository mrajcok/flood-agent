"""The time span every part of the synthetic data shares, and its daily load curve."""

import math

START_MS = 1767225600000  # 2026-01-01T00:00:00Z
DAYS = 7
MIN_MS = 60_000
TOTAL_MINUTES = DAYS * 24 * 60
END_MS = START_MS + DAYS * 86_400_000  # the end of the generated data; the eval runner's fixed "now"


def diurnal(minute: int) -> float:
    """Multiplier peaking mid-afternoon UTC, trough in the small hours."""
    h = (minute % 1440) / 60
    return 1 + 0.6 * math.sin(2 * math.pi * (h - 9) / 24)

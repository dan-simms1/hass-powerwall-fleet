"""Tesla app compatible reserve/SOE percentage helpers."""

from __future__ import annotations

from typing import Any

LOW_SOE_RESERVE_PERCENT = 5.0
USABLE_SOE_PERCENT = 100.0 - LOW_SOE_RESERVE_PERCENT


def raw_reserve_to_app_percent(value: Any) -> float | None:
    """Convert raw gateway reserve percent to Tesla app reserve percent.

    Powerwall keeps the bottom 5% of nominal capacity reserved for cell health.
    The Tesla app displays percentages over the usable 5-100% range, while the
    gateway config stores raw nominal percentages.
    """
    if not isinstance(value, (int, float)):
        return None
    app_value = (float(value) - LOW_SOE_RESERVE_PERCENT) / USABLE_SOE_PERCENT * 100
    return round(max(0.0, min(100.0, app_value)), 1)


def app_reserve_to_raw_percent(value: float) -> float:
    """Convert a Tesla app reserve percent to raw gateway config percent."""
    raw_value = float(value) / 100 * USABLE_SOE_PERCENT + LOW_SOE_RESERVE_PERCENT
    return round(max(LOW_SOE_RESERVE_PERCENT, min(100.0, raw_value)), 1)

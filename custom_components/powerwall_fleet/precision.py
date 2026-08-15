"""Central state-value rounding for Tesla Powerwall Local (Fleet).

Home Assistant fires ``state_changed`` only when an entity's state *string*
differs from the previous one, and each websocket client buffers those events
(4096 pending, then the connection is force-closed). The gateway reports raw
float precision — a state of charge comes back as ``97.027972027972`` — so at
source precision essentially every sensor "changes" on every poll even when
nothing meaningful moved, and a slow frontend client cannot keep up.

Rounding the *state value* is what stops those events. ``suggested_display_
precision`` does not: it is display-only and leaves the state untouched.

Precision is the ``ndigits`` argument to :func:`round`, so it may be negative —
``-1`` rounds to the nearest 10. It is chosen per measurement class so the
quantisation step sits at or below the gateway's own measurement accuracy;
anything finer is noise that only costs websocket traffic.

Everything is table-driven from this module so precision can be retuned in one
place without touching the sensor descriptions.
"""

from __future__ import annotations

from math import isfinite

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import (
    PERCENTAGE,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.helpers.typing import StateType

# Precision for a device class measured in its usual native unit.
_BY_DEVICE_CLASS: dict[SensorDeviceClass, int] = {
    # Power flows: 10 W steps. An 11.5 kW system's CTs are not accurate to
    # single watts, so 1 W steps carry no information — and because the raw
    # signal moves by tens of watts between polls, rounding to whole watts
    # would not silence a single update.
    SensorDeviceClass.POWER: -1,
    SensorDeviceClass.APPARENT_POWER: -1,
    SensorDeviceClass.REACTIVE_POWER: -1,
    # Mains voltage to 0.1 V and frequency to 0.01 Hz: both genuinely move at
    # that scale, so this trims float noise without hiding real excursions.
    SensorDeviceClass.VOLTAGE: 1,
    SensorDeviceClass.FREQUENCY: 2,
    SensorDeviceClass.CURRENT: 2,
    SensorDeviceClass.TEMPERATURE: 1,
    # State of charge to 0.1%. Fine enough for threshold automations and for
    # watching a charge or discharge progress in real time.
    SensorDeviceClass.BATTERY: 1,
    # Cumulative and stored energy arrive in Wh: 1 Wh steps, i.e. three
    # decimals once Home Assistant converts to kWh. This is deliberately the
    # finest rounding here — these counters feed Riemann-sum helpers and
    # long-term statistics, and over-rounding a slow increment would quantise
    # away real energy. Rounding a running *total* (rather than each
    # increment) also keeps the error bounded at ±0.5 Wh forever instead of
    # accumulating, and cannot make a monotonic counter go backwards.
    SensorDeviceClass.ENERGY: 0,
    SensorDeviceClass.ENERGY_STORAGE: 0,
    # Whole units of whatever the duration is measured in; see the per-unit
    # override below for durations reported in hours.
    SensorDeviceClass.DURATION: 0,
}

# Overrides for descriptions that report a larger unit than the device-class
# default assumes, so the physical step size stays the same.
_BY_UNIT: dict[tuple[SensorDeviceClass | None, str], int] = {
    (SensorDeviceClass.POWER, UnitOfPower.KILO_WATT): 2,  # 0.01 kW = 10 W
    (SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR): 3,  # 1 Wh
    (SensorDeviceClass.ENERGY_STORAGE, UnitOfEnergy.KILO_WATT_HOUR): 3,  # 1 Wh
    # Backup time remaining is in hours, so the whole-unit default would
    # quantise it to the hour. 0.1 h (6 min) matches what the sensor already
    # displays, and it is the single biggest churn win here: the estimate is
    # derived from load power, so at raw precision it moves on every poll.
    (SensorDeviceClass.DURATION, UnitOfTime.HOURS): 1,
    # Percentages that carry no device class (e.g. backup reserve).
    (None, PERCENTAGE): 1,
}


def precision_for(
    device_class: SensorDeviceClass | None, unit: str | None
) -> int | None:
    """Return the ``round`` precision for a measurement class, or None.

    None means "leave the value alone" — used for counts and any class not
    listed above.
    """
    if unit is not None and (ndigits := _BY_UNIT.get((device_class, unit))) is not None:
        return ndigits
    if device_class is None:
        return None
    return _BY_DEVICE_CLASS.get(device_class)


def round_state(
    value: StateType, device_class: SensorDeviceClass | None, unit: str | None
) -> StateType:
    """Round a sensor's native value to its class's meaningful precision.

    Non-numeric states (the gateway's many status strings), booleans and
    classes with no configured precision pass through untouched.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    if not isfinite(value):
        return value
    ndigits = precision_for(device_class, unit)
    if ndigits is None:
        return value
    rounded = round(float(value), ndigits)
    if ndigits <= 0:
        # A whole-number state reads as "591" rather than "591.0".
        return int(rounded)
    if rounded == 0:
        # Rounding tiny negative noise yields -0.0, whose state string
        # alternates against "0.0" and churns on its own.
        return 0.0
    return rounded

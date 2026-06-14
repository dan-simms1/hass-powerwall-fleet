"""Sensor platform for Powerwall Local (Fleet).

Entities are split across five coordinators (status, meters, battery SoC,
grid status, config) — each polled on its own cadence. The
``coordinator_attr`` field on a description names the runtime-data field
the entity binds to.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    UnitOfApparentPower,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfReactivePower,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    MANUFACTURER,
    MODEL,
    MODEL_EXPANSION,
    MODEL_MASTER,
)
from .coordinator import (
    MasterBlock,
    PowerwallRuntimeData,
    PowerwallFleetConfigEntry,
)
from .reserve import raw_reserve_to_app_percent


def _path(data: Any, *keys: Any) -> Any:
    """Walk nested mappings/lists; return None if a step is missing."""
    for key in keys:
        if isinstance(key, int):
            if not isinstance(data, list) or not -len(data) <= key < len(data):
                return None
            data = data[key]
        else:
            if not isinstance(data, Mapping):
                return None
            data = data.get(key)
    return data


@dataclass(frozen=True, kw_only=True)
class PowerwallFleetSensorDescription(SensorEntityDescription):
    """Describes a Powerwall Local (Fleet) sensor + the coordinator it reads from."""

    coordinator_attr: str
    value_fn: Callable[[Any], StateType]


@dataclass(frozen=True, kw_only=True)
class ExpansionSensorDescription(SensorEntityDescription):
    """Sensor that belongs to a battery expansion device (not the master).

    ``slot_index`` is the global components-payload slot for the expansion.
    """

    slot_index: int
    display_index: int
    value_fn: Callable[[dict[str, Any]], StateType]


POWER = SensorDeviceClass.POWER
ENERGY_STORE = SensorDeviceClass.ENERGY_STORAGE
ENERGY = SensorDeviceClass.ENERGY
MEAS = SensorStateClass.MEASUREMENT
TOTAL_INC = SensorStateClass.TOTAL_INCREASING
DIAG = EntityCategory.DIAGNOSTIC


# ── Helpers per coordinator payload ─────────────────────────────────────────


def _status_meter_power(location: str) -> Callable[[dict[str, Any]], StateType]:
    """`control.meterAggregates[location].realPowerW` from get_status."""

    def _fn(status: dict[str, Any]) -> StateType:
        meters = _path(status, "control", "meterAggregates")
        if not isinstance(meters, list):
            return None
        for meter in meters:
            if (
                isinstance(meter, Mapping)
                and str(meter.get("location", "")).upper() == location
            ):
                value = meter.get("realPowerW")
                return float(value) if isinstance(value, (int, float)) else None
        return None

    return _fn


def _percentage_charged(status: dict[str, Any]) -> StateType:
    remaining = _path(status, "control", "systemStatus", "nominalEnergyRemainingWh")
    full = _path(status, "control", "systemStatus", "nominalFullPackEnergyWh")
    if not isinstance(remaining, (int, float)) or not isinstance(full, (int, float)):
        return None
    if not full:
        return None
    return round(float(remaining) / float(full) * 100, 2)


def _system_time(status: dict[str, Any]) -> StateType:
    raw = _path(status, "system", "time")
    if not isinstance(raw, str):
        return None
    return dt_util.parse_datetime(raw)


def _alerts(status: dict[str, Any]) -> StateType:
    alerts = _path(status, "control", "alerts", "active")
    if not isinstance(alerts, list):
        return None
    return ", ".join(str(a) for a in alerts) or "none"


def _islander(field: str) -> Callable[[dict[str, Any]], StateType]:
    def _fn(status: dict[str, Any]) -> StateType:
        return _path(status, "esCan", "bus", "ISLANDER", "ISLAND_AcMeasurements", field)

    return _fn


def _islander_grid_connection(status: dict[str, Any]) -> StateType:
    return _path(
        status,
        "esCan",
        "bus",
        "ISLANDER",
        "ISLAND_GridConnection",
        "ISLAND_GridConnected",
    )


def _sync_meter(meter: str, field: str) -> Callable[[dict[str, Any]], StateType]:
    section = f"METER_{meter}_AcMeasurements"

    def _fn(status: dict[str, Any]) -> StateType:
        return _path(status, "esCan", "bus", "SYNC", section, field)

    return _fn


def _islanding(field: str) -> Callable[[dict[str, Any]], StateType]:
    def _fn(status: dict[str, Any]) -> StateType:
        value = _path(status, "control", "islanding", field)
        if isinstance(value, bool):
            return "on" if value else "off"
        return value

    return _fn


def _meters_field(location: str, field: str) -> Callable[[dict[str, Any]], StateType]:
    """Pull `meters_aggregates[location][field]`."""

    def _fn(data: dict[str, Any]) -> StateType:
        value = _path(data, location, field)
        return value if isinstance(value, (int, float)) else None

    return _fn


def _config_field(*path: str) -> Callable[[dict[str, Any]], StateType]:
    def _fn(cfg: dict[str, Any]) -> StateType:
        return _path(cfg, *path)

    return _fn


def _signal(component: Any, name: str) -> Any:
    """Return the numeric or text value of a named signal from a component entry.

    Components payload shape: ``{components: {<kind>: [{signals: [{name, value,
    textValue, boolValue}, ...]}, ...]}}``. Prefers ``value`` (number), then
    ``textValue``, then ``boolValue``.
    """
    if not isinstance(component, Mapping):
        return None
    for sig in component.get("signals") or ():
        if not isinstance(sig, Mapping) or sig.get("name") != name:
            continue
        if sig.get("value") is not None:
            return sig["value"]
        if sig.get("textValue") is not None:
            return sig["textValue"]
        return sig.get("boolValue")
    return None


def _component_at(kind: str, index: int) -> Callable[[dict[str, Any]], Any]:
    def _fn(data: dict[str, Any]) -> Any:
        items = _path(data, "components", kind)
        if not isinstance(items, list) or index >= len(items):
            return None
        return items[index]

    return _fn


def _component_slot_view(data: dict[str, Any], slot: int) -> dict[str, Any]:
    """Return a components payload where ``slot`` is visible as index 0."""
    if slot == 0:
        return data
    components = data.get("components")
    if not isinstance(components, Mapping):
        return data

    remapped: dict[str, Any] = {}
    for kind, items in components.items():
        if isinstance(items, list) and 0 <= slot < len(items):
            remapped[kind] = [items[slot]]
        else:
            remapped[kind] = []

    return {**data, "components": remapped}


def _component_signal(
    kind: str, index: int, name: str
) -> Callable[[dict[str, Any]], StateType]:
    getter = _component_at(kind, index)

    def _fn(data: dict[str, Any]) -> StateType:
        return _signal(getter(data), name)

    return _fn


def _component_field(
    kind: str, index: int, field: str
) -> Callable[[dict[str, Any]], StateType]:
    """Pull a top-level field (e.g. ``partNumber``) from a component entry."""
    getter = _component_at(kind, index)

    def _fn(data: dict[str, Any]) -> StateType:
        comp = getter(data)
        if not isinstance(comp, Mapping):
            return None
        value = comp.get(field)
        return value if value else None

    return _fn


def _bms_percentage_charged(slot: int) -> Callable[[dict[str, Any]], StateType]:
    """Compute SoC% from BMS_nominalEnergyRemaining / BMS_nominalFullPackEnergy."""
    remaining_fn = _component_signal("bms", slot, "BMS_nominalEnergyRemaining")
    full_fn = _component_signal("bms", slot, "BMS_nominalFullPackEnergy")

    def _fn(data: dict[str, Any]) -> StateType:
        remaining = remaining_fn(data)
        full = full_fn(data)
        if not isinstance(remaining, (int, float)) or not isinstance(full, (int, float)):
            return None
        if not full:
            return None
        return round(float(remaining) / float(full) * 100, 2)

    return _fn


def _pch_current(name: str) -> Callable[[dict[str, Any]], StateType]:
    """PCH PV currents come back as ~1e-16 noise when zero — round at the edge."""
    inner = _component_signal("pch", 0, name)

    def _fn(data: dict[str, Any]) -> StateType:
        value = inner(data)
        if isinstance(value, (int, float)) and abs(value) < 1e-3:
            return 0.0
        return value

    return _fn


# ── Sensor descriptions, grouped by coordinator ─────────────────────────────


_LOCATIONS = ("site", "battery", "load", "solar")


_METERS_AGGREGATE_SENSORS: tuple[PowerwallFleetSensorDescription, ...] = tuple(
    sensor
    for location in _LOCATIONS
    for sensor in (
        PowerwallFleetSensorDescription(
            key=f"{location}_apparent_power",
            translation_key=f"{location}_apparent_power",
            device_class=SensorDeviceClass.APPARENT_POWER,
            state_class=MEAS,
            native_unit_of_measurement=UnitOfApparentPower.VOLT_AMPERE,
            entity_registry_enabled_default=True,
            coordinator_attr="meters",
            value_fn=_meters_field(location, "instant_apparent_power"),
        ),
        PowerwallFleetSensorDescription(
            key=f"{location}_reactive_power",
            translation_key=f"{location}_reactive_power",
            device_class=SensorDeviceClass.REACTIVE_POWER,
            state_class=MEAS,
            native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
            entity_registry_enabled_default=True,
            coordinator_attr="meters",
            value_fn=_meters_field(location, "instant_reactive_power"),
        ),
        PowerwallFleetSensorDescription(
            key=f"{location}_voltage",
            translation_key=f"{location}_voltage",
            device_class=SensorDeviceClass.VOLTAGE,
            state_class=MEAS,
            native_unit_of_measurement=UnitOfElectricPotential.VOLT,
            entity_registry_enabled_default=True,
            coordinator_attr="meters",
            value_fn=_meters_field(location, "instant_average_voltage"),
        ),
        PowerwallFleetSensorDescription(
            key=f"{location}_current",
            translation_key=f"{location}_current",
            device_class=SensorDeviceClass.CURRENT,
            state_class=MEAS,
            native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
            entity_registry_enabled_default=True,
            coordinator_attr="meters",
            value_fn=_meters_field(location, "instant_total_current"),
        ),
        PowerwallFleetSensorDescription(
            key=f"{location}_frequency",
            translation_key=f"{location}_frequency",
            device_class=SensorDeviceClass.FREQUENCY,
            state_class=MEAS,
            native_unit_of_measurement=UnitOfFrequency.HERTZ,
            entity_registry_enabled_default=True,
            coordinator_attr="meters",
            value_fn=_meters_field(location, "frequency"),
        ),
        PowerwallFleetSensorDescription(
            key=f"{location}_energy_exported",
            translation_key=f"{location}_energy_exported",
            device_class=ENERGY,
            state_class=TOTAL_INC,
            native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
            suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            suggested_display_precision=2,
            coordinator_attr="meters",
            value_fn=_meters_field(location, "energy_exported"),
        ),
        PowerwallFleetSensorDescription(
            key=f"{location}_energy_imported",
            translation_key=f"{location}_energy_imported",
            device_class=ENERGY,
            state_class=TOTAL_INC,
            native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
            suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            suggested_display_precision=2,
            coordinator_attr="meters",
            value_fn=_meters_field(location, "energy_imported"),
        ),
    )
)


_STATUS_SENSORS: tuple[PowerwallFleetSensorDescription, ...] = (
    # Top-level power flows from control.meterAggregates
    PowerwallFleetSensorDescription(
        key="battery_power",
        translation_key="battery_power",
        device_class=POWER,
        state_class=MEAS,
        native_unit_of_measurement=UnitOfPower.WATT,
        coordinator_attr="status",
        value_fn=_status_meter_power("BATTERY"),
    ),
    PowerwallFleetSensorDescription(
        key="site_power",
        translation_key="site_power",
        device_class=POWER,
        state_class=MEAS,
        native_unit_of_measurement=UnitOfPower.WATT,
        coordinator_attr="status",
        value_fn=_status_meter_power("SITE"),
    ),
    PowerwallFleetSensorDescription(
        key="load_power",
        translation_key="load_power",
        device_class=POWER,
        state_class=MEAS,
        native_unit_of_measurement=UnitOfPower.WATT,
        coordinator_attr="status",
        value_fn=_status_meter_power("LOAD"),
    ),
    PowerwallFleetSensorDescription(
        key="solar_power",
        translation_key="solar_power",
        device_class=POWER,
        state_class=MEAS,
        native_unit_of_measurement=UnitOfPower.WATT,
        coordinator_attr="status",
        value_fn=_status_meter_power("SOLAR"),
    ),
    PowerwallFleetSensorDescription(
        key="solar_rgm_power",
        translation_key="solar_rgm_power",
        device_class=POWER,
        state_class=MEAS,
        native_unit_of_measurement=UnitOfPower.WATT,
        entity_registry_enabled_default=True,
        coordinator_attr="status",
        value_fn=_status_meter_power("SOLAR_RGM"),
    ),
    PowerwallFleetSensorDescription(
        key="generator_power",
        translation_key="generator_power",
        device_class=POWER,
        state_class=MEAS,
        native_unit_of_measurement=UnitOfPower.WATT,
        entity_registry_enabled_default=True,
        coordinator_attr="status",
        value_fn=_status_meter_power("GENERATOR"),
    ),
    PowerwallFleetSensorDescription(
        key="conductor_power",
        translation_key="conductor_power",
        device_class=POWER,
        state_class=MEAS,
        native_unit_of_measurement=UnitOfPower.WATT,
        entity_registry_enabled_default=True,
        coordinator_attr="status",
        value_fn=_status_meter_power("CONDUCTOR"),
    ),
    # Battery energy (computed SoC, plus raw remaining/full)
    PowerwallFleetSensorDescription(
        key="percentage_charged_computed",
        translation_key="percentage_charged_computed",
        device_class=SensorDeviceClass.BATTERY,
        state_class=MEAS,
        native_unit_of_measurement=PERCENTAGE,
        entity_registry_enabled_default=True,
        coordinator_attr="status",
        value_fn=_percentage_charged,
    ),
    PowerwallFleetSensorDescription(
        key="energy_remaining",
        translation_key="energy_remaining",
        device_class=ENERGY_STORE,
        state_class=MEAS,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        coordinator_attr="status",
        value_fn=lambda s: _path(
            s, "control", "systemStatus", "nominalEnergyRemainingWh"
        ),
    ),
    PowerwallFleetSensorDescription(
        key="full_pack_energy",
        translation_key="full_pack_energy",
        device_class=ENERGY_STORE,
        state_class=MEAS,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        coordinator_attr="status",
        value_fn=lambda s: _path(
            s, "control", "systemStatus", "nominalFullPackEnergyWh"
        ),
    ),
    # Islanding / gateway state diagnostics
    PowerwallFleetSensorDescription(
        key="island_mode",
        translation_key="island_mode",
        entity_category=DIAG,
        coordinator_attr="status",
        value_fn=_islanding("customerIslandMode"),
    ),
    PowerwallFleetSensorDescription(
        key="islander_grid_state",
        translation_key="islander_grid_state",
        entity_category=DIAG,
        coordinator_attr="status",
        value_fn=_islander("ISLAND_GridState"),
    ),
    PowerwallFleetSensorDescription(
        key="islander_grid_connection",
        translation_key="islander_grid_connection",
        entity_category=DIAG,
        coordinator_attr="status",
        value_fn=_islander_grid_connection,
    ),
    PowerwallFleetSensorDescription(
        key="active_alerts",
        translation_key="active_alerts",
        entity_category=DIAG,
        coordinator_attr="status",
        value_fn=_alerts,
    ),
    # ISLANDER per-phase frequency + voltage (Load + Main)
    *(
        PowerwallFleetSensorDescription(
            key=f"island_freq_l{phase}_{side.lower()}",
            translation_key=f"island_freq_l{phase}_{side.lower()}",
            device_class=SensorDeviceClass.FREQUENCY,
            state_class=MEAS,
            native_unit_of_measurement=UnitOfFrequency.HERTZ,
            entity_registry_enabled_default=True,
            coordinator_attr="status",
            value_fn=_islander(f"ISLAND_FreqL{phase}_{side}"),
        )
        for phase in (1, 2, 3)
        for side in ("Load", "Main")
    ),
    *(
        PowerwallFleetSensorDescription(
            key=f"island_voltage_l{phase}n_{side.lower()}",
            translation_key=f"island_voltage_l{phase}n_{side.lower()}",
            device_class=SensorDeviceClass.VOLTAGE,
            state_class=MEAS,
            native_unit_of_measurement=UnitOfElectricPotential.VOLT,
            entity_registry_enabled_default=True,
            coordinator_attr="status",
            value_fn=_islander(f"ISLAND_VL{phase}N_{side}"),
        )
        for phase in (1, 2, 3)
        for side in ("Load", "Main")
    ),
    # SYNC METER_X / METER_Y per-CT measurements
    *(
        sensor
        for meter in ("X", "Y")
        for ct in ("A", "B", "C")
        for sensor in (
            PowerwallFleetSensorDescription(
                key=f"meter_{meter.lower()}_ct{ct.lower()}_real_power",
                translation_key=f"meter_{meter.lower()}_ct{ct.lower()}_real_power",
                device_class=POWER,
                state_class=MEAS,
                native_unit_of_measurement=UnitOfPower.WATT,
                # Redundant raw per-CT CAN-bus reading; on for advanced diagnostics only.
                entity_registry_enabled_default=False,
                coordinator_attr="status",
                value_fn=_sync_meter(meter, f"METER_{meter}_CT{ct}_InstRealPower"),
            ),
            PowerwallFleetSensorDescription(
                key=f"meter_{meter.lower()}_ct{ct.lower()}_reactive_power",
                translation_key=f"meter_{meter.lower()}_ct{ct.lower()}_reactive_power",
                device_class=SensorDeviceClass.REACTIVE_POWER,
                state_class=MEAS,
                native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
                entity_registry_enabled_default=False,
                coordinator_attr="status",
                value_fn=_sync_meter(meter, f"METER_{meter}_CT{ct}_InstReactivePower"),
            ),
            PowerwallFleetSensorDescription(
                key=f"meter_{meter.lower()}_ct{ct.lower()}_current",
                translation_key=f"meter_{meter.lower()}_ct{ct.lower()}_current",
                device_class=SensorDeviceClass.CURRENT,
                state_class=MEAS,
                native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
                entity_registry_enabled_default=False,
                coordinator_attr="status",
                value_fn=_sync_meter(meter, f"METER_{meter}_CT{ct}_I"),
            ),
        )
    ),
    *(
        PowerwallFleetSensorDescription(
            key=f"meter_{meter.lower()}_vl{phase}n",
            translation_key=f"meter_{meter.lower()}_vl{phase}n",
            device_class=SensorDeviceClass.VOLTAGE,
            state_class=MEAS,
            native_unit_of_measurement=UnitOfElectricPotential.VOLT,
            entity_registry_enabled_default=False,
            coordinator_attr="status",
            value_fn=_sync_meter(meter, f"METER_{meter}_VL{phase}N"),
        )
        for meter in ("X", "Y")
        for phase in (1, 2, 3)
    ),
)


_BATTERY_SOE_SENSORS: tuple[PowerwallFleetSensorDescription, ...] = (
    PowerwallFleetSensorDescription(
        key="battery_soe",
        translation_key="battery_soe",
        device_class=SensorDeviceClass.BATTERY,
        state_class=MEAS,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
        coordinator_attr="battery_soe",
        value_fn=lambda v: v if isinstance(v, (int, float)) else None,
    ),
)


_GRID_STATUS_SENSORS: tuple[PowerwallFleetSensorDescription, ...] = (
    PowerwallFleetSensorDescription(
        key="grid_status",
        translation_key="grid_status",
        coordinator_attr="grid_status",
        value_fn=lambda v: v if isinstance(v, str) else None,
    ),
)


_CONFIG_SENSORS: tuple[PowerwallFleetSensorDescription, ...] = (
    PowerwallFleetSensorDescription(
        key="backup_reserve_percent",
        translation_key="backup_reserve_percent",
        state_class=MEAS,
        native_unit_of_measurement=PERCENTAGE,
        coordinator_attr="config",
        value_fn=lambda cfg: raw_reserve_to_app_percent(
            _path(cfg, "site_info", "backup_reserve_percent")
        ),
    ),
    PowerwallFleetSensorDescription(
        key="net_meter_mode",
        translation_key="net_meter_mode",
        entity_category=DIAG,
        coordinator_attr="config",
        value_fn=_config_field("site_info", "net_meter_mode"),
    ),
    PowerwallFleetSensorDescription(
        key="customer_preferred_export_rule",
        translation_key="customer_preferred_export_rule",
        entity_category=DIAG,
        coordinator_attr="config",
        value_fn=_config_field("site_info", "customer_preferred_export_rule"),
    ),
    PowerwallFleetSensorDescription(
        key="nominal_system_energy_ac",
        translation_key="nominal_system_energy_ac",
        device_class=ENERGY_STORE,
        state_class=MEAS,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        entity_category=DIAG,
        coordinator_attr="config",
        value_fn=_config_field("site_info", "nominal_system_energy_ac"),
    ),
    PowerwallFleetSensorDescription(
        key="nominal_system_power_ac",
        translation_key="nominal_system_power_ac",
        device_class=POWER,
        state_class=MEAS,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        entity_category=DIAG,
        coordinator_attr="config",
        value_fn=_config_field("site_info", "nominal_system_power_ac"),
    ),
    PowerwallFleetSensorDescription(
        key="grid_code",
        translation_key="grid_code",
        entity_category=DIAG,
        entity_registry_enabled_default=True,
        coordinator_attr="config",
        value_fn=_config_field("site_info", "grid_code"),
    ),
    PowerwallFleetSensorDescription(
        key="country",
        translation_key="country",
        entity_category=DIAG,
        entity_registry_enabled_default=True,
        coordinator_attr="config",
        value_fn=_config_field("site_info", "country"),
    ),
    PowerwallFleetSensorDescription(
        key="distributor",
        translation_key="distributor",
        entity_category=DIAG,
        entity_registry_enabled_default=True,
        coordinator_attr="config",
        value_fn=_config_field("site_info", "distributor"),
    ),
)


_PCH_STRINGS = ("a", "b", "c", "d", "e", "f")


# NOTE: every component-payload slot index in `_MASTER_COMPONENT_SENSORS`
# below is intentionally scoped to 0. `MasterBatterySensor` remaps each
# block's global component slot into position 0 before evaluating these
# descriptions. This preserves the existing block-0 entity keys while letting
# follower PW3 blocks use their own component slots.
_MASTER_COMPONENT_SENSORS: tuple[PowerwallFleetSensorDescription, ...] = (
    # Master battery — BMS energy
    PowerwallFleetSensorDescription(
        key="bms_0_energy_remaining",
        translation_key="bms_energy_remaining",
        device_class=ENERGY_STORE,
        state_class=MEAS,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        coordinator_attr="components",
        value_fn=_component_signal("bms", 0, "BMS_nominalEnergyRemaining"),
    ),
    PowerwallFleetSensorDescription(
        key="bms_0_full_pack_energy",
        translation_key="bms_full_pack_energy",
        device_class=ENERGY_STORE,
        state_class=MEAS,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        entity_category=DIAG,
        entity_registry_enabled_default=True,
        coordinator_attr="components",
        value_fn=_component_signal("bms", 0, "BMS_nominalFullPackEnergy"),
    ),
    PowerwallFleetSensorDescription(
        key="bms_0_percentage_charged",
        translation_key="bms_percentage_charged",
        device_class=SensorDeviceClass.BATTERY,
        state_class=MEAS,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
        coordinator_attr="components",
        value_fn=_bms_percentage_charged(0),
    ),
    # PCH AC measurements (master only — master arbitrates AC for the whole stack)
    PowerwallFleetSensorDescription(
        key="pch_ac_frequency",
        translation_key="pch_ac_frequency",
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=MEAS,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        suggested_display_precision=3,
        coordinator_attr="components",
        value_fn=_component_signal("pch", 0, "PCH_AcFrequency"),
    ),
    PowerwallFleetSensorDescription(
        key="pch_ac_voltage_ab",
        translation_key="pch_ac_voltage_ab",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=MEAS,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
        coordinator_attr="components",
        value_fn=_component_signal("pch", 0, "PCH_AcVoltageAB"),
    ),
    PowerwallFleetSensorDescription(
        key="pch_ac_voltage_an",
        translation_key="pch_ac_voltage_an",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=MEAS,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
        entity_registry_enabled_default=True,
        coordinator_attr="components",
        value_fn=_component_signal("pch", 0, "PCH_AcVoltageAN"),
    ),
    PowerwallFleetSensorDescription(
        key="pch_ac_voltage_bn",
        translation_key="pch_ac_voltage_bn",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=MEAS,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
        entity_registry_enabled_default=True,
        coordinator_attr="components",
        value_fn=_component_signal("pch", 0, "PCH_AcVoltageBN"),
    ),
    PowerwallFleetSensorDescription(
        key="pch_ac_real_power",
        translation_key="pch_ac_real_power",
        device_class=POWER,
        state_class=MEAS,
        native_unit_of_measurement=UnitOfPower.WATT,
        coordinator_attr="components",
        value_fn=_component_signal("pch", 0, "PCH_AcRealPowerAB"),
    ),
    PowerwallFleetSensorDescription(
        key="pch_battery_power",
        translation_key="pch_battery_power",
        device_class=POWER,
        state_class=MEAS,
        native_unit_of_measurement=UnitOfPower.WATT,
        coordinator_attr="components",
        value_fn=_component_signal("pch", 0, "PCH_BatteryPower"),
    ),
    PowerwallFleetSensorDescription(
        key="pch_pv_power_sum",
        translation_key="pch_pv_power_sum",
        device_class=POWER,
        state_class=MEAS,
        native_unit_of_measurement=UnitOfPower.WATT,
        coordinator_attr="components",
        value_fn=_component_signal("pch", 0, "PCH_SlowPvPowerSum"),
    ),
    # BAGGR diagnostics (master only — battery aggregator across all units)
    PowerwallFleetSensorDescription(
        key="baggr_batteries_connected",
        translation_key="baggr_batteries_connected",
        state_class=MEAS,
        entity_category=DIAG,
        coordinator_attr="components",
        value_fn=_component_signal("baggr", 0, "BAGGR_NumBatteriesConnected"),
    ),
    PowerwallFleetSensorDescription(
        key="baggr_batteries_expected",
        translation_key="baggr_batteries_expected",
        state_class=MEAS,
        entity_category=DIAG,
        entity_registry_enabled_default=True,
        coordinator_attr="components",
        value_fn=_component_signal("baggr", 0, "BAGGR_NumBatteriesExpected"),
    ),
    *(
        PowerwallFleetSensorDescription(
            key=f"pch_pv_voltage_{s}",
            translation_key=f"pch_pv_voltage_{s}",
            device_class=SensorDeviceClass.VOLTAGE,
            state_class=MEAS,
            native_unit_of_measurement=UnitOfElectricPotential.VOLT,
            suggested_display_precision=1,
            coordinator_attr="components",
            value_fn=_component_signal("pch", 0, f"PCH_PvVoltage{s.upper()}"),
        )
        for s in _PCH_STRINGS
    ),
    *(
        PowerwallFleetSensorDescription(
            key=f"pch_pv_current_{s}",
            translation_key=f"pch_pv_current_{s}",
            device_class=SensorDeviceClass.CURRENT,
            state_class=MEAS,
            native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
            suggested_display_precision=2,
            coordinator_attr="components",
            value_fn=_pch_current(f"PCH_PvCurrent{s.upper()}"),
        )
        for s in _PCH_STRINGS
    ),
    # PCH state strings (master only)
    PowerwallFleetSensorDescription(
        key="pch_state",
        translation_key="pch_state",
        entity_category=DIAG,
        coordinator_attr="components",
        value_fn=_component_signal("pch", 0, "PCH_State"),
    ),
    PowerwallFleetSensorDescription(
        key="pch_ac_mode",
        translation_key="pch_ac_mode",
        entity_category=DIAG,
        coordinator_attr="components",
        value_fn=_component_signal("pch", 0, "PCH_AcMode"),
    ),
    PowerwallFleetSensorDescription(
        key="pch_dcdc_state_a",
        translation_key="pch_dcdc_state_a",
        entity_category=DIAG,
        entity_registry_enabled_default=True,
        coordinator_attr="components",
        value_fn=_component_signal("pch", 0, "PCH_DcdcState_A"),
    ),
    PowerwallFleetSensorDescription(
        key="pch_dcdc_state_b",
        translation_key="pch_dcdc_state_b",
        entity_category=DIAG,
        entity_registry_enabled_default=True,
        coordinator_attr="components",
        value_fn=_component_signal("pch", 0, "PCH_DcdcState_B"),
    ),
    *(
        PowerwallFleetSensorDescription(
            key=f"pch_pv_state_{s}",
            translation_key=f"pch_pv_state_{s}",
            entity_category=DIAG,
            entity_registry_enabled_default=True,
            coordinator_attr="components",
            value_fn=_component_signal("pch", 0, f"PCH_PvState_{s.upper()}"),
        )
        for s in _PCH_STRINGS
    ),
    # BAGGR state + per-slot battery connection status (master only)
    PowerwallFleetSensorDescription(
        key="baggr_state",
        translation_key="baggr_state",
        entity_category=DIAG,
        coordinator_attr="components",
        value_fn=_component_signal("baggr", 0, "BAGGR_State"),
    ),
    PowerwallFleetSensorDescription(
        key="baggr_operation_request",
        translation_key="baggr_operation_request",
        entity_category=DIAG,
        coordinator_attr="components",
        value_fn=_component_signal("baggr", 0, "BAGGR_OperationRequest"),
    ),
    *(
        PowerwallFleetSensorDescription(
            key=f"baggr_batt_connection_status_{n}",
            translation_key=f"baggr_batt_connection_status_{n}",
            entity_category=DIAG,
            coordinator_attr="components",
            value_fn=_component_signal(
                "baggr", 0, f"BAGGR_LOG_BattConnectionStatus{n}"
            ),
        )
        for n in range(4)
    ),
    # HVP for the master battery (slot 0)
    PowerwallFleetSensorDescription(
        key="hvp_0_state",
        translation_key="hvp_state",
        entity_category=DIAG,
        coordinator_attr="components",
        value_fn=_component_signal("hvp", 0, "HVP_State"),
    ),
    PowerwallFleetSensorDescription(
        key="hvp_0_part_number",
        translation_key="hvp_part_number",
        entity_category=DIAG,
        entity_registry_enabled_default=True,
        coordinator_attr="components",
        value_fn=_component_field("hvp", 0, "partNumber"),
    ),
    PowerwallFleetSensorDescription(
        key="hvp_0_serial_number",
        translation_key="hvp_serial_number",
        entity_category=DIAG,
        entity_registry_enabled_default=True,
        coordinator_attr="components",
        value_fn=_component_field("hvp", 0, "serialNumber"),
    ),
)


_SITE_SENSORS: tuple[PowerwallFleetSensorDescription, ...] = (
    *_STATUS_SENSORS,
    *_METERS_AGGREGATE_SENSORS,
    *_BATTERY_SOE_SENSORS,
    *_GRID_STATUS_SENSORS,
    *_CONFIG_SENSORS,
)


def _block_expansion_descriptions(
    block: MasterBlock,
) -> tuple[tuple[str, ExpansionSensorDescription], ...]:
    """Per-expansion sensor descriptions for one master block.

    Returns ``(expansion_din, description)`` pairs. The components-payload
    slot is global across the components payload and may be interleaved with
    follower Powerwall slots.
    """
    out: list[tuple[str, ExpansionSensorDescription]] = []
    for offset, (din, slot) in enumerate(
        zip(block.expansion_dins, block.expansion_slots, strict=False)
    ):
        display_index = block.first_expansion_display_index + offset
        out.extend(
            (din, desc)
            for desc in (
                ExpansionSensorDescription(
                    key=f"bms_{slot}_energy_remaining",
                    translation_key="bms_energy_remaining",
                    device_class=ENERGY_STORE,
                    state_class=MEAS,
                    native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                    suggested_display_precision=2,
                    slot_index=slot,
                    display_index=display_index,
                    value_fn=_component_signal(
                        "bms", slot, "BMS_nominalEnergyRemaining"
                    ),
                ),
                ExpansionSensorDescription(
                    key=f"bms_{slot}_full_pack_energy",
                    translation_key="bms_full_pack_energy",
                    device_class=ENERGY_STORE,
                    state_class=MEAS,
                    native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                    suggested_display_precision=2,
                    entity_category=DIAG,
                    entity_registry_enabled_default=True,
                    slot_index=slot,
                    display_index=display_index,
                    value_fn=_component_signal(
                        "bms", slot, "BMS_nominalFullPackEnergy"
                    ),
                ),
                ExpansionSensorDescription(
                    key=f"bms_{slot}_percentage_charged",
                    translation_key="bms_percentage_charged",
                    device_class=SensorDeviceClass.BATTERY,
                    state_class=MEAS,
                    native_unit_of_measurement=PERCENTAGE,
                    suggested_display_precision=1,
                    slot_index=slot,
                    display_index=display_index,
                    value_fn=_bms_percentage_charged(slot),
                ),
                ExpansionSensorDescription(
                    key=f"hvp_{slot}_state",
                    translation_key="hvp_state",
                    entity_category=DIAG,
                    slot_index=slot,
                    display_index=display_index,
                    value_fn=_component_signal("hvp", slot, "HVP_State"),
                ),
                ExpansionSensorDescription(
                    key=f"hvp_{slot}_part_number",
                    translation_key="hvp_part_number",
                    entity_category=DIAG,
                    entity_registry_enabled_default=True,
                    slot_index=slot,
                    display_index=display_index,
                    value_fn=_component_field("hvp", slot, "partNumber"),
                ),
                ExpansionSensorDescription(
                    key=f"hvp_{slot}_serial_number",
                    translation_key="hvp_serial_number",
                    entity_category=DIAG,
                    entity_registry_enabled_default=True,
                    slot_index=slot,
                    display_index=display_index,
                    value_fn=_component_field("hvp", slot, "serialNumber"),
                ),
            )
        )
    return tuple(out)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PowerwallFleetConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Powerwall Local (Fleet) sensors."""
    runtime = entry.runtime_data
    entities: list[CoordinatorEntity[DataUpdateCoordinator[Any]]] = [
        PowerwallFleetSensor(runtime, description) for description in _SITE_SENSORS
    ]
    for block in runtime.master_blocks:
        entities.extend(
            MasterBatterySensor(runtime, block, description)
            for description in _MASTER_COMPONENT_SENSORS
        )
        entities.extend(
            ExpansionSensor(runtime, block, expansion_din, description)
            for expansion_din, description in _block_expansion_descriptions(block)
        )
    async_add_entities(entities)


class PowerwallFleetSensor(CoordinatorEntity[DataUpdateCoordinator[Any]], SensorEntity):
    """A Powerwall Local (Fleet) sensor bound to one of the per-endpoint coordinators."""

    _attr_has_entity_name = True
    entity_description: PowerwallFleetSensorDescription

    def __init__(
        self,
        runtime: PowerwallRuntimeData,
        description: PowerwallFleetSensorDescription,
    ) -> None:
        coordinator: DataUpdateCoordinator[Any] = getattr(
            runtime, description.coordinator_attr
        )
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{runtime.din}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, runtime.din)},
            name=coordinator.config_entry.title,
            manufacturer=MANUFACTURER,
            model=MODEL,
            serial_number=runtime.din,
            sw_version=runtime.firmware_version,
        )

    @property
    def native_value(self) -> StateType:
        return self.entity_description.value_fn(self.coordinator.data)

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()


class MasterBatterySensor(
    CoordinatorEntity[DataUpdateCoordinator[Any]], SensorEntity
):
    """A sensor for a master Powerwall battery (one per ``MasterBlock``).

    Master batteries are modelled as children of the site/gateway device so
    per-battery readings are scoped to the battery, not bundled onto the site.
    """

    _attr_has_entity_name = True
    entity_description: PowerwallFleetSensorDescription

    def __init__(
        self,
        runtime: PowerwallRuntimeData,
        block: MasterBlock,
        description: PowerwallFleetSensorDescription,
    ) -> None:
        super().__init__(runtime.components)
        self._block = block
        self.entity_description = description
        # For block 0 we keep the gateway-DIN-scoped unique-id pattern so
        # entities created before the per-battery refactor migrate cleanly.
        self._attr_unique_id = (
            f"{runtime.din}_{description.key}"
            if block.block_index == 0
            else f"{block.device_din}_{description.key}"
        )
        serial = (
            block.physical_din.rsplit("--", 1)[-1]
            if block.physical_din
            else None
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, block.device_din)},
            name="Powerwall" if block.role == "leader" else "Powerwall follower",
            manufacturer=MANUFACTURER,
            model=MODEL_MASTER,
            serial_number=serial,
            via_device=(DOMAIN, runtime.din),
        )

    @property
    def native_value(self) -> StateType:
        data = _component_slot_view(self.coordinator.data, self._block.component_slot)
        return self.entity_description.value_fn(data)

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()


class ExpansionSensor(CoordinatorEntity[DataUpdateCoordinator[Any]], SensorEntity):
    """A sensor that belongs to a battery-expansion device on a PW3 stack.

    Expansion devices are linked back to their owning master via
    ``via_device`` so they show up as children of that specific Powerwall
    in the device registry.
    """

    _attr_has_entity_name = True
    entity_description: ExpansionSensorDescription

    def __init__(
        self,
        runtime: PowerwallRuntimeData,
        block: MasterBlock,
        expansion_din: str,
        description: ExpansionSensorDescription,
    ) -> None:
        super().__init__(runtime.components)
        self.entity_description = description
        # DINs look like "<partNumber>--<serialNumber>"; the serial is what
        # users see on the unit sticker.
        serial = expansion_din.rsplit("--", 1)[-1]
        self._attr_unique_id = f"{expansion_din}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, expansion_din)},
            name=f"Powerwall expansion {description.display_index}",
            manufacturer=MANUFACTURER,
            model=MODEL_EXPANSION,
            serial_number=serial,
            via_device=(DOMAIN, block.device_din),
        )

    @property
    def native_value(self) -> StateType:
        return self.entity_description.value_fn(self.coordinator.data)

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

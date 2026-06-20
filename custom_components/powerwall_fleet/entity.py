"""Shared entity base for Tesla Powerwall Local (Fleet) control platforms."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import CONF_GATEWAY_HOST, DOMAIN, MANUFACTURER, MODEL
from .coordinator import PowerwallFleetConfigEntry, PowerwallRuntimeData

# Append " Local" to every device name. The cloud Tesla Fleet integration models
# the same site/Powerwall, so without this our entity_ids would collide with its
# (e.g. both want `sensor.<site>_battery_power`). Putting "Local" in the device
# name means Home Assistant's normal entity_id generation always produces a
# `..._local_...` id — keeping the area/room prefix HA adds, and never clashing
# with Tesla Fleet, with no need to force entity_ids by hand.
LOCAL_SUFFIX = "Local"


def local_device_name(base: str | None) -> str | None:
    """Return ``"<base> Local"`` (or None) for a Tesla-Fleet-safe device name."""
    return f"{base} {LOCAL_SUFFIX}" if base else None


def gateway_configuration_url(entry: PowerwallFleetConfigEntry | None) -> str | None:
    """Clickable link to the Powerwall's local web UI, from the gateway host."""
    host = entry.data.get(CONF_GATEWAY_HOST) if entry else None
    return f"https://{host}" if host else None


def config_path(data: Any, *keys: str) -> Any:
    """Walk dotted-path keys through a nested mapping, returning None if absent."""
    for key in keys:
        if not isinstance(data, Mapping):
            return None
        data = data.get(key)
    return data


class PowerwallFleetEntity(CoordinatorEntity[DataUpdateCoordinator[Any]]):
    """Base class wiring unique_id, device_info, and runtime data."""

    _attr_has_entity_name = True

    def __init__(
        self,
        runtime: PowerwallRuntimeData,
        coordinator: DataUpdateCoordinator[Any],
        description: EntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self.runtime = runtime
        self._attr_unique_id = f"{runtime.din}_{description.key}"
        title = coordinator.config_entry.title if coordinator.config_entry else None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, runtime.din)},
            name=local_device_name(title),
            manufacturer=MANUFACTURER,
            model=MODEL,
            serial_number=runtime.din,
            sw_version=runtime.firmware_version,
            configuration_url=gateway_configuration_url(coordinator.config_entry),
        )

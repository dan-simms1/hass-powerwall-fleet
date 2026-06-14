"""Shared entity base for Powerwall Local (Fleet) control platforms."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import PowerwallRuntimeData


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
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, runtime.din)},
            name=coordinator.config_entry.title if coordinator.config_entry else None,
            manufacturer=MANUFACTURER,
            model=MODEL,
            serial_number=runtime.din,
            sw_version=runtime.firmware_version,
        )

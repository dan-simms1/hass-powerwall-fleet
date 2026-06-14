"""Shared entity base for Powerwall Local (Fleet) control platforms."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.number import NumberEntityDescription
from homeassistant.components.select import SelectEntityDescription
from homeassistant.components.switch import SwitchEntityDescription
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from homeassistant.util import slugify

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import PowerwallRuntimeData

# Site-device control entities share the "Volt Vader"-style site name with the
# cloud Tesla Fleet integration, so their auto-generated entity_ids collide.
# Map each control description back to its platform domain so we can force a
# `<site>_local_<key>` entity_id that never clashes with Tesla Fleet's.
_LOCAL_CONTROL_DOMAINS: tuple[tuple[type, str], ...] = (
    (NumberEntityDescription, "number"),
    (SelectEntityDescription, "select"),
    (SwitchEntityDescription, "switch"),
)


def local_entity_id(domain: str, title: str | None, key: str) -> str | None:
    """Build a Tesla-Fleet-safe `<domain>.<site>_local_<key>` entity_id."""
    return f"{domain}.{slugify(title)}_local_{key}" if title else None


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
            name=title,
            manufacturer=MANUFACTURER,
            model=MODEL,
            serial_number=runtime.din,
            sw_version=runtime.firmware_version,
        )
        domain = next(
            (d for cls, d in _LOCAL_CONTROL_DOMAINS if isinstance(description, cls)),
            None,
        )
        if domain and (eid := local_entity_id(domain, title, description.key)):
            self.entity_id = eid

"""Diagnostics support for Tesla Powerwall Local (Fleet)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_GATEWAY_HOST, CONF_GATEWAY_PASSWORD
from .coordinator import PowerwallFleetConfigEntry

# Redacted by key, recursively, across the whole payload. Covers the gateway
# credentials plus the device identifiers (DINs / serials / public keys / site
# ids) that appear throughout the raw gateway payloads, so a shared diagnostics
# bundle does not leak hardware identity.
TO_REDACT = {
    CONF_GATEWAY_HOST,
    CONF_GATEWAY_PASSWORD,
    "din",
    "DIN",
    "vin",
    "device_din",
    "physical_din",
    "expansion_dins",
    "serialNumber",
    "serial_number",
    "device_serial",
    "short_id",
    "publicKey",
    "public_key",
    "energy_site_id",
    "site_id",
    "githash",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PowerwallFleetConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry (with device identifiers redacted)."""
    runtime = entry.runtime_data

    def _health(coordinator: Any) -> dict[str, Any]:
        return {
            "last_update_success": coordinator.last_update_success,
            "last_exception": (
                repr(coordinator.last_exception)
                if coordinator.last_exception
                else None
            ),
            "interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
        }

    coordinators = {
        "status": runtime.status,
        "meters": runtime.meters,
        "battery_soe": runtime.battery_soe,
        "grid_status": runtime.grid_status,
        "config": runtime.config,
        "backup_events": runtime.backup_events,
        "components": runtime.components,
    }

    data = {
        "entry": {
            "title": entry.title,
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "din": runtime.din,
        "firmware_version": runtime.firmware_version,
        "master_blocks": [
            {
                "block_index": block.block_index,
                "component_slot": block.component_slot,
                "device_din": block.device_din,
                "physical_din": block.physical_din,
                "role": block.role,
                "expansion_dins": list(block.expansion_dins),
                "expansion_slots": list(block.expansion_slots),
                "first_expansion_slot": block.first_expansion_slot,
            }
            for block in runtime.master_blocks
        ],
        "coordinator_health": {name: _health(c) for name, c in coordinators.items()},
        "coordinators": {name: c.data for name, c in coordinators.items()},
    }
    return async_redact_data(data, TO_REDACT)

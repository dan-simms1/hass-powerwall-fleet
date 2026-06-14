"""Diagnostics support for Powerwall Local (Fleet)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_GATEWAY_HOST, CONF_GATEWAY_PASSWORD
from .coordinator import PowerwallFleetConfigEntry

TO_REDACT = {CONF_GATEWAY_PASSWORD, CONF_GATEWAY_HOST}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PowerwallFleetConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime = entry.runtime_data
    return {
        "entry": {
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
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
        "coordinators": {
            "status": runtime.status.data,
            "meters": runtime.meters.data,
            "battery_soe": runtime.battery_soe.data,
            "grid_status": runtime.grid_status.data,
            "config": runtime.config.data,
            "backup_events": runtime.backup_events.data,
            "components": runtime.components.data,
        },
    }

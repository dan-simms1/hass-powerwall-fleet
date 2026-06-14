"""Select platform for Powerwall Local (Fleet)."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import EXPORT_RULES, OPERATION_MODES
from .coordinator import PowerwallRuntimeData, PowerwallFleetConfigEntry
from .entity import PowerwallFleetEntity, config_path


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PowerwallFleetConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Powerwall Local (Fleet) select entities."""
    runtime = entry.runtime_data
    async_add_entities(
        [
            OperationModeSelect(runtime),
            ExportRuleSelect(runtime),
        ]
    )


class OperationModeSelect(PowerwallFleetEntity, SelectEntity):
    """Powerwall operation mode."""

    _attr_options = list(OPERATION_MODES)

    def __init__(self, runtime: PowerwallRuntimeData) -> None:
        super().__init__(
            runtime,
            runtime.config,
            SelectEntityDescription(
                key="operation_mode",
                translation_key="operation_mode",
            ),
        )

    @property
    def current_option(self) -> str | None:
        value = config_path(self.coordinator.data, "default_real_mode")
        return value if value in self._attr_options else None

    async def async_select_option(self, option: str) -> None:
        await self.runtime.client.write_config({"default_real_mode": option})
        await self.coordinator.async_request_refresh()


class ExportRuleSelect(PowerwallFleetEntity, SelectEntity):
    """Customer-preferred export rule."""

    _attr_options = list(EXPORT_RULES)

    def __init__(self, runtime: PowerwallRuntimeData) -> None:
        super().__init__(
            runtime,
            runtime.config,
            SelectEntityDescription(
                key="export_rule",
                translation_key="export_rule",
            ),
        )

    @property
    def current_option(self) -> str | None:
        value = config_path(
            self.coordinator.data, "site_info", "customer_preferred_export_rule"
        )
        return value if value in self._attr_options else None

    async def async_select_option(self, option: str) -> None:
        await self.runtime.client.write_config(
            {"site_info.customer_preferred_export_rule": option}
        )
        await self.coordinator.async_request_refresh()

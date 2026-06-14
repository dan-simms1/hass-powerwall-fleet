"""Switch platform for Tesla Powerwall Local (Fleet)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import MANUAL_BACKUP_DEFAULT_SECONDS
from .coordinator import PowerwallRuntimeData, PowerwallFleetConfigEntry
from .entity import PowerwallFleetEntity, config_path


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PowerwallFleetConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tesla Powerwall Local (Fleet) switch entities."""
    runtime = entry.runtime_data
    async_add_entities(
        [
            AllowGridChargingSwitch(runtime),
            StormModeSwitch(runtime),
            GridServicesSwitch(runtime),
            ManualBackupSwitch(runtime),
            GridConnectedSwitch(runtime),
        ]
    )


class AllowGridChargingSwitch(PowerwallFleetEntity, SwitchEntity):
    """Allow charging Powerwall from the grid (inverted of `disallow_charge_from_grid_with_solar_installed`)."""

    def __init__(self, runtime: PowerwallRuntimeData) -> None:
        super().__init__(
            runtime,
            runtime.config,
            SwitchEntityDescription(
                key="allow_grid_charging",
                translation_key="allow_grid_charging",
            ),
        )

    @property
    def is_on(self) -> bool | None:
        value = config_path(
            self.coordinator.data,
            "components",
            "disallow_charge_from_grid_with_solar_installed",
        )
        return (not value) if isinstance(value, bool) else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._write(False)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._write(True)

    async def _write(self, disallow: bool) -> None:
        await self.runtime.client.write_config(
            {"components.disallow_charge_from_grid_with_solar_installed": disallow}
        )
        await self.coordinator.async_request_refresh()


class _BoolConfigSwitch(PowerwallFleetEntity, SwitchEntity):
    """Generic bool switch backed by a single dotted config.json key."""

    _attr_entity_category = EntityCategory.CONFIG
    _config_path: tuple[str, ...]
    _dotted_key: str

    def __init__(
        self,
        runtime: PowerwallRuntimeData,
        key: str,
        config_path_parts: tuple[str, ...],
    ) -> None:
        super().__init__(
            runtime,
            runtime.config,
            SwitchEntityDescription(key=key, translation_key=key),
        )
        self._config_path = config_path_parts
        self._dotted_key = ".".join(config_path_parts)

    @property
    def is_on(self) -> bool | None:
        value = config_path(self.coordinator.data, *self._config_path)
        return value if isinstance(value, bool) else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

    async def _set(self, value: bool) -> None:
        await self.runtime.client.write_config({self._dotted_key: value})
        await self.coordinator.async_request_refresh()


class StormModeSwitch(_BoolConfigSwitch):
    """Storm watch / storm mode (experimental on this local build)."""

    def __init__(self, runtime: PowerwallRuntimeData) -> None:
        super().__init__(runtime, "storm_mode", ("user_settings", "storm_mode_enabled"))


class GridServicesSwitch(_BoolConfigSwitch):
    """Grid services / VPP participation (experimental on this local build)."""

    def __init__(self, runtime: PowerwallRuntimeData) -> None:
        super().__init__(
            runtime, "grid_services", ("components", "grid_services_enabled")
        )


class ManualBackupSwitch(PowerwallFleetEntity, SwitchEntity):
    """Toggle manual backup mode (max reserve) on or off immediately."""

    def __init__(self, runtime: PowerwallRuntimeData) -> None:
        super().__init__(
            runtime,
            runtime.backup_events,
            SwitchEntityDescription(
                key="manual_backup",
                translation_key="manual_backup",
            ),
        )

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        if not isinstance(data, dict):
            return None
        manual = data.get("manual_backup")
        if not isinstance(manual, dict):
            return False
        active = manual.get("active")
        return active if isinstance(active, bool) else False

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.runtime.client.schedule_max_backup(
            duration_seconds=MANUAL_BACKUP_DEFAULT_SECONDS
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.runtime.client.cancel_max_backup()
        await self.coordinator.async_request_refresh()


class GridConnectedSwitch(PowerwallFleetEntity, SwitchEntity):
    """Connect or disconnect the gateway from the grid via the islanding contactor.

    State reflects the actual contactor position (`islanding.contactorClosed`),
    not the commanded state — local builds will ACK the command even when the
    gateway does not actuate the contactor, so the switch can snap back if the
    request was not honoured.
    """

    def __init__(self, runtime: PowerwallRuntimeData) -> None:
        super().__init__(
            runtime,
            runtime.status,
            SwitchEntityDescription(
                key="grid_connected",
                translation_key="grid_connected",
            ),
        )

    @property
    def is_on(self) -> bool | None:
        value = config_path(self.coordinator.data, "control", "islanding", "contactorClosed")
        return value if isinstance(value, bool) else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.runtime.client.reconnect_grid()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.runtime.client.go_off_grid()
        await self.coordinator.async_request_refresh()

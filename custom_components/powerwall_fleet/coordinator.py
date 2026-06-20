"""DataUpdateCoordinators for the Tesla Powerwall Local (Fleet) integration.

Each gateway endpoint gets its own coordinator so it can be polled at its
own cadence — `get_status` and `get_meters_aggregates` are fast, the
direct SoC and grid-status endpoints are medium, and `get_config` is
slow (it only changes when the user edits gateway settings).

All coordinators share a single :class:`PowerwallClient`, so the
underlying transport / auth state is reused.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from aiopowerwall import (
    BackupEventsPayload,
    PowerwallAuthenticationError,
    PowerwallClient,
    PowerwallConnectionError,
    PowerwallError,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_SCAN_PROFILE,
    DEFAULT_SCAN_PROFILE,
    DOMAIN,
    LOGGER,
    MIN_SCAN_SECONDS,
    SCAN_BACKUP_EVENTS_SECONDS,
    SCAN_BATTERY_SOE_SECONDS,
    SCAN_COMPONENTS_SECONDS,
    SCAN_CONFIG_SECONDS,
    SCAN_GRID_STATUS_SECONDS,
    SCAN_METERS_SECONDS,
    SCAN_PROFILE_MULTIPLIERS,
    SCAN_STATUS_SECONDS,
)

type PowerwallFleetConfigEntry = ConfigEntry["PowerwallRuntimeData"]


@dataclass(frozen=True)
class MasterBlock:
    """One Powerwall block and its expansions.

    Mirrors a single entry in ``get_config['battery_blocks']``: the master
    is the Powerwall that owns ``expansion_dins[]`` (which may be empty for
    masters with no expansions installed). ``block_index`` is the position
    in ``battery_blocks``. ``component_slot`` is the slot in the components
    payload arrays (``bms[]``/``hvp[]``/``pch[]``/``baggr[]``).

    PW3 follower units appear as additional Powerwall blocks. Tesla's component
    slot order is not guaranteed to put all Powerwalls before all expansions, so
    each master/expansion stores the exact component slot it should read.
    """

    block_index: int
    component_slot: int
    device_din: str
    physical_din: str | None
    role: str
    expansion_dins: tuple[str, ...]
    expansion_slots: tuple[int, ...]
    first_expansion_slot: int
    first_expansion_display_index: int


@dataclass
class PowerwallRuntimeData:
    """Per-entry runtime data shared across platforms."""

    client: PowerwallClient
    din: str
    firmware_version: str | None
    status: StatusCoordinator
    meters: MetersCoordinator
    battery_soe: BatterySoeCoordinator
    grid_status: GridStatusCoordinator
    config: ConfigCoordinator
    backup_events: BackupEventsCoordinator
    components: ComponentsCoordinator
    master_blocks: tuple[MasterBlock, ...]


class _BasePowerwallCoordinator[T](DataUpdateCoordinator[T]):
    """Shared error translation for all per-endpoint coordinators."""

    _label: str

    def __init__(
        self,
        hass: HomeAssistant,
        entry: PowerwallFleetConfigEntry,
        client: PowerwallClient,
        interval_seconds: int,
    ) -> None:
        multiplier = SCAN_PROFILE_MULTIPLIERS.get(
            entry.options.get(CONF_SCAN_PROFILE, DEFAULT_SCAN_PROFILE), 1.0
        )
        interval = max(MIN_SCAN_SECONDS, round(interval_seconds * multiplier))
        super().__init__(
            hass,
            LOGGER,
            name=f"{DOMAIN}_{self._label}_{entry.entry_id}",
            update_interval=timedelta(seconds=interval),
            config_entry=entry,
        )
        self.client = client

    async def _fetch(self) -> T:  # pragma: no cover - overridden
        raise NotImplementedError

    async def _async_update_data(self) -> T:
        try:
            return await self._fetch()
        except PowerwallAuthenticationError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (PowerwallConnectionError, PowerwallError) as err:
            raise UpdateFailed(f"{self._label} failed: {err}") from err


class StatusCoordinator(_BasePowerwallCoordinator[dict[str, Any]]):
    """Polls the gateway DeviceController status payload."""

    _label = "status"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: PowerwallFleetConfigEntry,
        client: PowerwallClient,
    ) -> None:
        super().__init__(hass, entry, client, SCAN_STATUS_SECONDS)

    async def _fetch(self) -> dict[str, Any]:
        return await self.client.get_status()


class MetersCoordinator(_BasePowerwallCoordinator[dict[str, Any]]):
    """Polls `/api/meters/aggregates` for per-location power + energy totals."""

    _label = "meters"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: PowerwallFleetConfigEntry,
        client: PowerwallClient,
    ) -> None:
        super().__init__(hass, entry, client, SCAN_METERS_SECONDS)

    async def _fetch(self) -> dict[str, Any]:
        return await self.client.get_meters_aggregates()


class BatterySoeCoordinator(_BasePowerwallCoordinator[float]):
    """Polls `/api/system_status/soe` for the directly-reported SoC."""

    _label = "battery_soe"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: PowerwallFleetConfigEntry,
        client: PowerwallClient,
    ) -> None:
        super().__init__(hass, entry, client, SCAN_BATTERY_SOE_SECONDS)

    async def _fetch(self) -> float:
        return await self.client.get_battery_soe()


class GridStatusCoordinator(_BasePowerwallCoordinator[str]):
    """Polls `/api/system_status/grid_status` for the high-level grid state."""

    _label = "grid_status"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: PowerwallFleetConfigEntry,
        client: PowerwallClient,
    ) -> None:
        super().__init__(hass, entry, client, SCAN_GRID_STATUS_SECONDS)

    async def _fetch(self) -> str:
        return await self.client.get_grid_status()


class ConfigCoordinator(_BasePowerwallCoordinator[dict[str, Any]]):
    """Polls gateway `config.json` infrequently.

    The user can change settings like `backup_reserve_percent` either
    through this integration or via the Tesla app, so we refresh on a
    slow cadence rather than treating it as static.
    """

    _label = "config"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: PowerwallFleetConfigEntry,
        client: PowerwallClient,
    ) -> None:
        super().__init__(hass, entry, client, SCAN_CONFIG_SECONDS)

    async def _fetch(self) -> dict[str, Any]:
        return await self.client.get_config()


class BackupEventsCoordinator(_BasePowerwallCoordinator[BackupEventsPayload]):
    """Polls active/scheduled manual backup events."""

    _label = "backup_events"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: PowerwallFleetConfigEntry,
        client: PowerwallClient,
    ) -> None:
        super().__init__(hass, entry, client, SCAN_BACKUP_EVENTS_SECONDS)

    async def _fetch(self) -> BackupEventsPayload:
        return await self.client.get_backup_events()


class ComponentsCoordinator(_BasePowerwallCoordinator[dict[str, Any]]):
    """Polls `get_components` for Powerwall 3 per-unit telemetry.

    Returns per-component lists (`baggr`, `bms`, `hvp`, `pch`, `pws`) where
    list index 0 is the master and 1+ are battery expansions.
    """

    _label = "components"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: PowerwallFleetConfigEntry,
        client: PowerwallClient,
    ) -> None:
        super().__init__(hass, entry, client, SCAN_COMPONENTS_SECONDS)

    async def _fetch(self) -> dict[str, Any]:
        return await self.client.get_components()

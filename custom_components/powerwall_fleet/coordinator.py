"""DataUpdateCoordinators for the Tesla Powerwall Local (Fleet) integration.

Each gateway endpoint gets its own coordinator so it can be polled at its
own cadence — the direct SoC endpoint is what drives automations, `get_status`
is medium, and `get_config` is slow (it only changes when the user edits
gateway settings).

All coordinators share a single :class:`PowerwallClient`, so the underlying
transport / auth state is reused.

They do **not** each run their own timer. The gateway is typically a Wi-Fi
client in power-save mode, where the cost is waking the radio rather than
moving bytes, and independent timers drag it awake many times a minute at
unrelated phases. Instead :class:`PollScheduler` drives every coordinator from
one tick, so a cycle is a single burst of requests over a single kept-alive
connection.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from aiohttp import ClientSession
from aiopowerwall import (
    BackupEventsPayload,
    PowerwallAuthenticationError,
    PowerwallClient,
    PowerwallConnectionError,
    PowerwallError,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_track_time_interval
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
    """Per-entry runtime data shared across platforms.

    The three optional coordinators are ``None`` when their endpoint group is
    switched off, and platforms skip the entities that read them.
    """

    client: PowerwallClient
    session: ClientSession
    din: str
    firmware_version: str | None
    status: StatusCoordinator
    battery_soe: BatterySoeCoordinator
    grid_status: GridStatusCoordinator
    config: ConfigCoordinator
    meters: MetersCoordinator | None
    backup_events: BackupEventsCoordinator | None
    components: ComponentsCoordinator | None
    master_blocks: tuple[MasterBlock, ...]


class _BasePowerwallCoordinator[T](DataUpdateCoordinator[T]):
    """Shared error translation for all per-endpoint coordinators.

    ``update_interval`` is left unset: refreshes are driven by
    :class:`PollScheduler` so that every coordinator falls due on the same
    tick. The interval each one *wants* is kept in ``poll_interval`` for the
    scheduler to read. Writes still call ``async_request_refresh()`` directly,
    which is unaffected by this and stays immediate.
    """

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
        self.poll_interval = max(
            MIN_SCAN_SECONDS, round(interval_seconds * multiplier)
        )
        super().__init__(
            hass,
            LOGGER,
            name=f"{DOMAIN}_{self._label}_{entry.entry_id}",
            update_interval=None,
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


class PollScheduler:
    """Refreshes every coordinator from one shared tick.

    The tick is the shortest interval any coordinator asked for, and each
    coordinator refreshes every Nth tick, N being its own interval rounded to
    a whole number of ticks. Because the configured intervals are all
    multiples of the shortest one, that rounding is exact and the slower
    coordinators always fall due on a tick the faster ones are already using —
    so a cycle is one burst of requests, and the gateway's radio wakes once
    for all of them instead of once per coordinator.

    Coordinators refresh concurrently within a burst; the client's connector
    is limited to a single connection per host, so the requests queue on one
    kept-alive connection rather than opening one each.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        coordinators: tuple[_BasePowerwallCoordinator[Any], ...],
    ) -> None:
        self._hass = hass
        self._tick_seconds = min(c.poll_interval for c in coordinators)
        self._members = tuple(
            (coordinator, max(1, round(coordinator.poll_interval / self._tick_seconds)))
            for coordinator in coordinators
        )
        # Setup has just refreshed everything, so that counts as tick 0.
        self._tick = 0

    @property
    def tick_seconds(self) -> int:
        """Seconds between bursts."""
        return self._tick_seconds

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        """Begin ticking; returns the unsubscribe callback."""
        LOGGER.debug(
            "Polling every %ds: %s",
            self._tick_seconds,
            ", ".join(
                f"{coordinator._label} every {every} tick(s)"
                for coordinator, every in self._members
            ),
        )
        return async_track_time_interval(
            self._hass,
            self._async_tick,
            timedelta(seconds=self._tick_seconds),
            name=f"{DOMAIN}_poll",
        )

    async def _async_tick(self, _now: Any) -> None:
        self._tick += 1
        due = [
            coordinator
            for coordinator, every in self._members
            if self._tick % every == 0
        ]
        if due:
            await asyncio.gather(*(c.async_refresh() for c in due))

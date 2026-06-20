"""The Tesla Powerwall Local (Fleet) integration."""

from __future__ import annotations

import asyncio
from pathlib import Path

from aiopowerwall import (
    PowerwallAuthenticationError,
    PowerwallClient,
    PowerwallConnectionError,
    PowerwallError,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_GATEWAY_HOST,
    CONF_GATEWAY_PASSWORD,
    KEY_FILENAME,
    LOGGER,
    MASTER_BATTERY_DIN_SUFFIX,
)
from .coordinator import (
    BackupEventsCoordinator,
    BatterySoeCoordinator,
    ComponentsCoordinator,
    ConfigCoordinator,
    GridStatusCoordinator,
    MasterBlock,
    MetersCoordinator,
    PowerwallRuntimeData,
    PowerwallFleetConfigEntry,
    StatusCoordinator,
)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(
    hass: HomeAssistant, entry: PowerwallFleetConfigEntry
) -> bool:
    """Set up Tesla Powerwall Local (Fleet) from a config entry."""
    key_path = Path(hass.config.path(KEY_FILENAME))
    try:
        key_pem = await hass.async_add_executor_job(key_path.read_bytes)
    except OSError as err:
        raise ConfigEntryNotReady(
            f"RSA key file {key_path} is unavailable: {err}"
        ) from err

    client = PowerwallClient(
        host=entry.data[CONF_GATEWAY_HOST],
        gateway_password=entry.data[CONF_GATEWAY_PASSWORD],
        rsa_private_key_pem=key_pem,
        session=async_get_clientsession(hass),
    )

    try:
        din = await client.connect()
        firmware_details = await client.get_firmware_details()
    except PowerwallAuthenticationError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except (PowerwallConnectionError, PowerwallError) as err:
        raise ConfigEntryNotReady(f"Gateway unreachable: {err}") from err

    status = StatusCoordinator(hass, entry, client)
    meters = MetersCoordinator(hass, entry, client)
    battery_soe = BatterySoeCoordinator(hass, entry, client)
    grid_status = GridStatusCoordinator(hass, entry, client)
    config = ConfigCoordinator(hass, entry, client)
    backup_events = BackupEventsCoordinator(hass, entry, client)
    components = ComponentsCoordinator(hass, entry, client)

    await asyncio.gather(
        status.async_config_entry_first_refresh(),
        meters.async_config_entry_first_refresh(),
        battery_soe.async_config_entry_first_refresh(),
        grid_status.async_config_entry_first_refresh(),
        config.async_config_entry_first_refresh(),
        backup_events.async_config_entry_first_refresh(),
        components.async_config_entry_first_refresh(),
    )

    master_blocks = _master_blocks(config.data, status.data, components.data, din)

    entry.runtime_data = PowerwallRuntimeData(
        client=client,
        din=din,
        firmware_version=firmware_details["system"]["version"]["text"] or None,
        status=status,
        meters=meters,
        battery_soe=battery_soe,
        grid_status=grid_status,
        config=config,
        backup_events=backup_events,
        components=components,
        master_blocks=master_blocks,
    )
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_update))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_reload_on_update(
    hass: HomeAssistant, entry: PowerwallFleetConfigEntry
) -> None:
    """Reload the entry when its options (e.g. polling profile) change."""
    await hass.config_entries.async_reload(entry.entry_id)


def _block_din(block: dict) -> str | None:
    """Return the physical DIN/VIN carried by a battery block."""
    for key in ("din", "vin"):
        value = block.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _serial_from_din(din: str | None) -> str | None:
    """Return the serial suffix from a DIN/VIN-like identifier."""
    if not din:
        return None
    return din.rsplit("--", 1)[-1]


def _expansion_din(expansion: dict) -> str | None:
    """Return the physical DIN/VIN carried by a battery expansion."""
    for key in ("din", "vin"):
        value = expansion.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _status_battery_dins(status_payload: dict) -> tuple[str, ...]:
    """Return Powerwall DINs reported by the live status payload."""
    blocks = (
        status_payload.get("control", {}).get("batteryBlocks", [])
        if isinstance(status_payload.get("control"), dict)
        else []
    )
    return tuple(
        block["din"]
        for block in blocks
        if isinstance(block, dict)
        and isinstance(block.get("din"), str)
        and block["din"]
    )


def _path(data: object, *keys: object) -> object:
    """Walk nested mappings/lists; return None if a step is missing."""
    for key in keys:
        if isinstance(key, int):
            if not isinstance(data, list) or not -len(data) <= key < len(data):
                return None
            data = data[key]
        else:
            if not isinstance(data, dict):
                return None
            data = data.get(key)
    return data


def _signal_value(component: object, name: str) -> object:
    """Return a component signal value/text/bool by name."""
    if not isinstance(component, dict):
        return None
    for signal in component.get("signals") or ():
        if not isinstance(signal, dict) or signal.get("name") != name:
            continue
        if signal.get("value") is not None:
            return signal["value"]
        if signal.get("textValue") is not None:
            return signal["textValue"]
        return signal.get("boolValue")
    return None


def _signal_float(component: object, name: str) -> float | None:
    """Return a numeric component signal as float."""
    value = _signal_value(component, name)
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _component_serial(components_payload: dict, slot: int) -> str | None:
    """Return the best serial number for a component slot."""
    for kind in ("hvp", "bms", "pch", "baggr"):
        value = _path(components_payload, "components", kind, slot, "serialNumber")
        if isinstance(value, str) and value:
            return value
    return None


def _bms_component_slots(components_payload: dict) -> tuple[int, ...]:
    """Return component slots that look like real BMS modules."""
    bms_items = _path(components_payload, "components", "bms")
    if not isinstance(bms_items, list):
        return ()

    slots: list[int] = []
    for slot, component in enumerate(bms_items):
        full = _signal_value(component, "BMS_nominalFullPackEnergy")
        if not isinstance(full, (int, float)) or full <= 0:
            continue
        slots.append(slot)
    return tuple(slots)


def _status_full_pack_energy_kwh(status_payload: dict) -> float | None:
    """Return aggregate system full-pack energy in kWh from status."""
    value = _path(
        status_payload,
        "control",
        "systemStatus",
        "nominalFullPackEnergyWh",
    )
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value) / 1000


def _ghost_filtered_bms_component_slots(
    components_payload: dict,
    status_payload: dict,
) -> tuple[int, ...]:
    """Return BMS slots after dropping phantom expansion candidates.

    Tesla can leave registered-but-not-installed expansion rows behind. They
    look like BMS rows with plausible full capacity, no serial number, and
    near-zero remaining energy. Only remove them when the aggregate system
    capacity already matches the kept rows, which avoids dropping real
    no-serial packs whose remaining-energy signal is stale.
    """
    slots = list(_bms_component_slots(components_payload))
    aggregate_full_kwh = _status_full_pack_energy_kwh(status_payload)
    if not slots or aggregate_full_kwh is None:
        return tuple(slots)

    ghost_slots: list[int] = []
    for slot in slots:
        if _component_serial(components_payload, slot):
            continue
        bms = _path(components_payload, "components", "bms", slot)
        full = _signal_float(bms, "BMS_nominalFullPackEnergy")
        remaining = _signal_float(bms, "BMS_nominalEnergyRemaining")
        if full is None or full <= 0 or remaining is None:
            continue
        if remaining < 0.5 or remaining / full < 0.05:
            ghost_slots.append(slot)

    if not ghost_slots:
        return tuple(slots)

    kept_slots = [slot for slot in slots if slot not in ghost_slots]
    kept_full_kwh = 0.0
    for slot in kept_slots:
        bms = _path(components_payload, "components", "bms", slot)
        kept_full_kwh += _signal_float(bms, "BMS_nominalFullPackEnergy") or 0.0

    full_delta = abs(aggregate_full_kwh - kept_full_kwh) / aggregate_full_kwh
    if kept_full_kwh > 0 and full_delta < 0.10:
        LOGGER.warning(
            "Dropping %d ghost Powerwall expansion slot(s): aggregate %.2f kWh "
            "matches real BMS slot sum %.2f kWh",
            len(ghost_slots),
            aggregate_full_kwh,
            kept_full_kwh,
        )
        return tuple(kept_slots)

    return tuple(slots)


def _matched_expansion_slots(
    components_payload: dict,
    expansion_dins: tuple[str, ...],
    bms_slots: tuple[int, ...] | None = None,
) -> dict[str, int]:
    """Match configured expansion DINs to component slots by serial suffix."""
    serial_to_din = {
        serial: din
        for din in expansion_dins
        if (serial := _serial_from_din(din))
    }
    if not serial_to_din:
        return {}

    matched: dict[str, int] = {}
    candidate_slots = (
        bms_slots
        if bms_slots is not None
        else _bms_component_slots(components_payload)
    )
    for slot in candidate_slots:
        serial = _component_serial(components_payload, slot)
        if serial in serial_to_din and serial_to_din[serial] not in matched:
            matched[serial_to_din[serial]] = slot
    return matched


def _choose_follower_component_slots(
    components_payload: dict,
    powerwall_count: int,
    expansion_slots: set[int],
    bms_slots: tuple[int, ...] | None = None,
) -> tuple[int, ...]:
    """Choose component slots for follower PW3 blocks.

    Known expansion slots are protected first. When follower BMS rows have no
    serial number, prefer those rows; otherwise fall back to the last available
    component slots to preserve the old positional behaviour.
    """
    follower_count = max(0, powerwall_count - 1)
    if follower_count == 0:
        return ()

    slots = list(
        bms_slots
        if bms_slots is not None
        else _bms_component_slots(components_payload)
    )
    if not slots:
        return tuple(range(1, powerwall_count))

    candidates = [
        slot for slot in slots if slot != 0 and slot not in expansion_slots
    ]
    no_serial_candidates = [
        slot for slot in candidates if not _component_serial(components_payload, slot)
    ]
    pool = (
        no_serial_candidates
        if len(no_serial_candidates) >= follower_count
        else candidates
    )
    chosen = list(pool[-follower_count:])
    fallback_slot = 1
    while len(chosen) < follower_count:
        if fallback_slot not in chosen and fallback_slot not in expansion_slots:
            chosen.append(fallback_slot)
        fallback_slot += 1
    return tuple(sorted(chosen))


def _inferred_expansion_din(
    components_payload: dict,
    gateway_din: str,
    slot: int,
) -> str:
    """Build a stable synthetic DIN for an expansion omitted from config."""
    serial = _component_serial(components_payload, slot)
    return f"inferred-expansion--{serial}" if serial else f"{gateway_din}_expansion_{slot}"


def _master_blocks(
    config_payload: dict,
    status_payload: dict,
    components_payload: dict,
    gateway_din: str,
) -> tuple[MasterBlock, ...]:
    """Build per-Powerwall block metadata from config/status payloads.

    Each entry in ``battery_blocks`` is one Powerwall master with its own
    optional ``battery_expansions[]``. Block 0's HA device identifier is
    derived from the gateway DIN for backwards compatibility with existing
    deployments; later blocks prefer their physical DIN when available.
    """
    blocks = [
        block
        for block in (config_payload.get("battery_blocks") or [])
        if isinstance(block, dict)
    ]
    known_dins = {_block_din(block) for block in blocks}
    for din in _status_battery_dins(status_payload):
        if din not in known_dins:
            blocks.append({"din": din})
            known_dins.add(din)

    if not blocks:
        blocks.append({"din": gateway_din})
    powerwall_count = len(blocks)

    expansion_dins_by_block = [
        tuple(
            din
            for expansion in block.get("battery_expansions") or []
            if isinstance(expansion, dict)
            and (din := _expansion_din(expansion))
        )
        for block in blocks
    ]
    configured_expansion_dins = tuple(
        din for dins in expansion_dins_by_block for din in dins
    )
    bms_slots = list(
        _ghost_filtered_bms_component_slots(components_payload, status_payload)
    )
    matched_expansion_slots = _matched_expansion_slots(
        components_payload,
        configured_expansion_dins,
        tuple(bms_slots),
    )
    follower_slots = _choose_follower_component_slots(
        components_payload,
        powerwall_count,
        set(matched_expansion_slots.values()),
        tuple(bms_slots),
    )

    assigned_slots = {0, *follower_slots, *matched_expansion_slots.values()}
    fallback_expansion_slots = [
        slot for slot in bms_slots if slot not in assigned_slots
    ]
    next_unobserved_expansion_slot = max(powerwall_count, len(bms_slots))

    def next_expansion_slot() -> int | None:
        nonlocal next_unobserved_expansion_slot
        if fallback_expansion_slots:
            slot = fallback_expansion_slots.pop(0)
        elif not bms_slots:
            # No component truth is available. Preserve the old config-driven
            # device shape until the gateway starts returning BMS rows.
            slot = next_unobserved_expansion_slot
            next_unobserved_expansion_slot += 1
        else:
            return None
        assigned_slots.add(slot)
        return slot

    expansion_slots_by_block: list[tuple[int, ...]] = []
    filtered_expansion_dins_by_block: list[tuple[str, ...]] = []
    for expansion_dins in expansion_dins_by_block:
        kept_dins: list[str] = []
        slots: list[int] = []
        for din in expansion_dins:
            slot = matched_expansion_slots.get(din)
            if slot is None:
                slot = next_expansion_slot()
            if slot is None:
                LOGGER.warning(
                    "Dropping configured Powerwall expansion %s because no "
                    "real BMS component slot is present",
                    din,
                )
                continue
            assigned_slots.add(slot)
            kept_dins.append(din)
            slots.append(slot)
        filtered_expansion_dins_by_block.append(tuple(kept_dins))
        expansion_slots_by_block.append(tuple(slots))
    expansion_dins_by_block = filtered_expansion_dins_by_block

    inferred_expansion_slots = [
        slot for slot in bms_slots if slot not in assigned_slots
    ]
    if inferred_expansion_slots:
        first_dins = list(expansion_dins_by_block[0])
        first_slots = list(expansion_slots_by_block[0])
        for slot in inferred_expansion_slots:
            first_dins.append(
                _inferred_expansion_din(components_payload, gateway_din, slot)
            )
            first_slots.append(slot)
            assigned_slots.add(slot)
        expansion_dins_by_block[0] = tuple(first_dins)
        expansion_slots_by_block[0] = tuple(first_slots)

    next_expansion_display_index = 1
    out: list[MasterBlock] = []
    for i, block in enumerate(blocks):
        expansion_dins = expansion_dins_by_block[i]
        expansion_slots = expansion_slots_by_block[i]
        physical_din = _block_din(block)
        device_din = (
            f"{gateway_din}{MASTER_BATTERY_DIN_SUFFIX}"
            if i == 0
            else physical_din or f"{gateway_din}_battery_{i}"
        )
        component_slot = (
            0
            if i == 0
            else follower_slots[i - 1]
            if i - 1 < len(follower_slots)
            else i
        )
        first_expansion_slot = (
            expansion_slots[0]
            if expansion_slots
            else max(powerwall_count, len(bms_slots))
        )
        out.append(
            MasterBlock(
                block_index=i,
                component_slot=component_slot,
                device_din=device_din,
                physical_din=physical_din,
                role="leader" if i == 0 else "follower",
                expansion_dins=expansion_dins,
                expansion_slots=expansion_slots,
                first_expansion_slot=first_expansion_slot,
                first_expansion_display_index=next_expansion_display_index,
            )
        )
        next_expansion_display_index += len(expansion_dins)
    return tuple(out)


async def async_unload_entry(
    hass: HomeAssistant, entry: PowerwallFleetConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

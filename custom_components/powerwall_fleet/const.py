"""Constants for the Tesla Powerwall Local (Fleet) integration."""

from __future__ import annotations

import logging

DOMAIN = "powerwall_fleet"

LOGGER = logging.getLogger(__package__)

CONF_PARENT_ENTRY_ID = "parent_entry_id"
CONF_ENERGY_SITE_ID = "energy_site_id"
CONF_GATEWAY_PASSWORD = "gateway_password"
CONF_GATEWAY_HOST = "gateway_host"

KEY_FILENAME = "powerwall_fleet.key"
KEY_PAIRING_POLL_INTERVAL = 3
KEY_PAIRING_POLL_ATTEMPTS = 5

# Poll intervals, in seconds. The gateway is typically a Wi-Fi client in
# power-save mode, so what costs it is radio wake-ups, not bytes: keep the
# fast cadence only where a value drives a decision. Every interval is a
# multiple of the slowest common tick so the coordinators coincide instead of
# waking the radio at their own separate phases — see POLL_TICK_SECONDS.
SCAN_BATTERY_SOE_SECONDS = 30  # state of charge: drives charging automations
SCAN_STATUS_SECONDS = 60
SCAN_GRID_STATUS_SECONDS = 300  # grid up/down is a rare event
SCAN_CONFIG_SECONDS = 1800  # only changes when someone edits gateway settings
SCAN_METERS_SECONDS = 300
SCAN_BACKUP_EVENTS_SECONDS = 300
SCAN_COMPONENTS_SECONDS = 300

# aiohttp's default keepalive_timeout (~15s) is shorter than every poll gap
# above, so without this the pooled connection is always dead before the next
# poll and each one pays a fresh TCP + TLS setup — about 95% of the traffic.
# The gateway honours keep-alive; hold connections open across a whole cycle.
KEEPALIVE_TIMEOUT_SECONDS = 300
# One connection per host, so a burst of due coordinators queues on the single
# open connection rather than opening one each.
CONNECTION_LIMIT_PER_HOST = 1

# Optional polling profile (options flow): scales every coordinator interval.
CONF_SCAN_PROFILE = "scan_profile"
SCAN_PROFILE_FAST = "fast"
SCAN_PROFILE_NORMAL = "normal"
SCAN_PROFILE_RELAXED = "relaxed"
DEFAULT_SCAN_PROFILE = SCAN_PROFILE_NORMAL
SCAN_PROFILE_MULTIPLIERS: dict[str, float] = {
    SCAN_PROFILE_FAST: 0.5,
    SCAN_PROFILE_NORMAL: 1.0,
    SCAN_PROFILE_RELAXED: 2.0,
}
MIN_SCAN_SECONDS = 5

# Optional endpoint groups (options flow). These back entities that most
# installs never read — per-location meter aggregates, PW3 per-unit telemetry
# and manual backup events — and together they are the majority of the
# gateway's request load. Off by default; turning one on brings its entities
# back with their original unique_ids, so history is preserved either way.
CONF_ENABLE_METERS = "enable_meters"
CONF_ENABLE_COMPONENTS = "enable_components"
CONF_ENABLE_BACKUP_EVENTS = "enable_backup_events"
OPTIONAL_COORDINATOR_OPTIONS: tuple[str, ...] = (
    CONF_ENABLE_METERS,
    CONF_ENABLE_COMPONENTS,
    CONF_ENABLE_BACKUP_EVENTS,
)
DEFAULT_ENABLE_OPTIONAL_COORDINATORS = False

MANUFACTURER = "Tesla"
MODEL = "Powerwall 3 (Fleet local)"
MODEL_MASTER = "Powerwall 3"
MODEL_EXPANSION = "Powerwall 3 Expansion"

MASTER_BATTERY_DIN_SUFFIX = "_battery_master"

OPERATION_MODE_SELF_CONSUMPTION = "self_consumption"
OPERATION_MODE_AUTONOMOUS = "autonomous"
OPERATION_MODE_BACKUP = "backup"
OPERATION_MODES: tuple[str, ...] = (
    OPERATION_MODE_SELF_CONSUMPTION,
    OPERATION_MODE_AUTONOMOUS,
    OPERATION_MODE_BACKUP,
)

EXPORT_RULE_BATTERY_OK = "battery_ok"
EXPORT_RULE_PV_ONLY = "pv_only"
EXPORT_RULE_NEVER = "never"
EXPORT_RULES: tuple[str, ...] = (
    EXPORT_RULE_BATTERY_OK,
    EXPORT_RULE_PV_ONLY,
    EXPORT_RULE_NEVER,
)

MANUAL_BACKUP_DEFAULT_SECONDS = 7200

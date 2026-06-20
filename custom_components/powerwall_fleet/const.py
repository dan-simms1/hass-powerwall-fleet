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

SCAN_STATUS_SECONDS = 10
SCAN_METERS_SECONDS = 10
SCAN_BATTERY_SOE_SECONDS = 30
SCAN_GRID_STATUS_SECONDS = 30
SCAN_CONFIG_SECONDS = 600
SCAN_BACKUP_EVENTS_SECONDS = 30
SCAN_COMPONENTS_SECONDS = 30

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

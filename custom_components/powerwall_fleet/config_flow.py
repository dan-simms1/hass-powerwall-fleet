"""Config flow for Powerwall Local (Fleet).

Consumes the core `tesla_fleet` integration's runtime data rather than running
its own OAuth flow — the user picks one of their existing Tesla Fleet entries
and one energy site under it, then completes the local gateway pairing. One
config entry per site; if the user has multiple sites they re-run the flow.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.components.tesla_fleet import TeslaFleetConfigEntry
from homeassistant.components.tesla_fleet.models import TeslaFleetEnergyData
from homeassistant.config_entries import (
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from tesla_fleet_api.const import (
    AuthorizedClientKeyType,
    AuthorizedClientState,
    AuthorizedClientType,
)
from tesla_fleet_api.exceptions import TeslaFleetError
from tesla_fleet_api.tesla.tesla import Tesla

from .const import (
    CONF_ENERGY_SITE_ID,
    CONF_GATEWAY_HOST,
    CONF_GATEWAY_PASSWORD,
    CONF_PARENT_ENTRY_ID,
    DOMAIN,
    KEY_FILENAME,
    KEY_PAIRING_POLL_ATTEMPTS,
    KEY_PAIRING_POLL_INTERVAL,
    LOGGER,
)


def _extract_host(networking_status: dict[str, Any] | None) -> str:
    """Extract an IPv4 from a Tesla Fleet networking status payload.

    Checks ``eth`` then ``wifi``, preferring an interface flagged
    ``active_route``, then any interface with an ``ipv4_config.address``.
    """
    if not networking_status:
        return ""
    payload = networking_status.get("response", networking_status)
    if not isinstance(payload, Mapping):
        return ""

    def _addr(iface: Any) -> str:
        if not isinstance(iface, Mapping):
            return ""
        ipv4 = iface.get("ipv4_config")
        addr = ipv4.get("address") if isinstance(ipv4, Mapping) else None
        return addr if isinstance(addr, str) else ""

    interfaces = [payload.get(name) for name in ("eth", "wifi")]
    for iface in interfaces:
        if isinstance(iface, Mapping) and iface.get("active_route") and (a := _addr(iface)):
            return a
    for iface in interfaces:
        if (a := _addr(iface)):
            return a
    return ""


def _normalize_b64(value: Any) -> str:
    return "".join(value.split()) if isinstance(value, str) else ""


def _iter_clients(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, Mapping):
        return []
    for key in ("authorized_clients", "authorizedClients", "clients", "response"):
        if key in payload:
            found = _iter_clients(payload[key])
            if found:
                return found
    for value in payload.values():
        if isinstance(value, (list, Mapping)):
            found = _iter_clients(value)
            if found:
                return found
    return []


_VERIFIED_VALUES: tuple[Any, ...] = (
    AuthorizedClientState.VERIFIED,
    int(AuthorizedClientState.VERIFIED),
    "VERIFIED",
    "AUTHORIZED_CLIENT_STATE_VERIFIED",
)


def _find_client_for_key(list_response: Any, public_key_b64: str) -> Mapping[str, Any] | None:
    target = _normalize_b64(public_key_b64)
    for client in _iter_clients(list_response):
        if not isinstance(client, Mapping):
            continue
        key = client.get("public_key") or client.get("publicKey") or ""
        if _normalize_b64(key) == target:
            return client
    return None


def _is_verified(client: Mapping[str, Any] | None) -> bool:
    if client is None:
        return False
    state = client.get("state") or client.get("authorized_client_state")
    return state in _VERIFIED_VALUES


class PowerwallFleetConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config flow for Powerwall Local (Fleet), backed by a Tesla Fleet entry."""

    VERSION = 1

    def __init__(self) -> None:
        self._parent_entry: TeslaFleetConfigEntry | None = None
        self._key_pem: bytes | None = None
        self._public_key_b64: str = ""
        self._public_key_der: bytes = b""
        self._site: dict[str, Any] | None = None

    # ----- account / site selection ----------------------------------------------

    def _loaded_parents(self) -> list[TeslaFleetConfigEntry]:
        return [
            e
            for e in self.hass.config_entries.async_entries("tesla_fleet")
            if e.state is ConfigEntryState.LOADED
        ]

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        parents = self._loaded_parents()
        if not parents:
            return self.async_abort(reason="tesla_fleet_not_loaded")

        if len(parents) == 1:
            self._parent_entry = parents[0]
            return await self.async_step_pick_site()

        if user_input is not None:
            self._parent_entry = self.hass.config_entries.async_get_entry(
                user_input[CONF_PARENT_ENTRY_ID]
            )
            if self._parent_entry is None:
                return self.async_abort(reason="tesla_fleet_not_loaded")
            return await self.async_step_pick_site()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PARENT_ENTRY_ID): vol.In(
                        {e.entry_id: e.title for e in parents}
                    )
                }
            ),
        )

    async def async_step_pick_site(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._parent_entry is not None
        energysites: list[TeslaFleetEnergyData] = self._parent_entry.runtime_data.energysites
        if not energysites:
            return self.async_abort(reason="no_sites")

        configured = {
            e.data[CONF_ENERGY_SITE_ID]
            for e in self.hass.config_entries.async_entries(DOMAIN)
        }
        available = [s for s in energysites if s.id not in configured]
        if not available:
            return self.async_abort(reason="already_configured")

        if len(available) == 1:
            return await self._select_site(available[0])

        if user_input is not None:
            chosen = next(
                (s for s in available if str(s.id) == user_input[CONF_ENERGY_SITE_ID]),
                None,
            )
            if chosen is None:
                return self.async_abort(reason="no_sites")
            return await self._select_site(chosen)

        choices = {str(s.id): self._site_label(s) for s in available}
        return self.async_show_form(
            step_id="pick_site",
            data_schema=vol.Schema(
                {vol.Required(CONF_ENERGY_SITE_ID): vol.In(choices)}
            ),
        )

    async def _select_site(self, s: TeslaFleetEnergyData) -> ConfigFlowResult:
        await self.async_set_unique_id(str(s.id))
        self._abort_if_unique_id_configured()
        self._site = await self._site_meta(s)
        if (aborted := await self._ensure_key_loaded()) is not None:
            return aborted

        try:
            response = await self._site["api"].list_authorized_clients()
        except TeslaFleetError as err:
            LOGGER.warning(
                "list_authorized_clients failed for site %s: %s", s.id, err
            )
            response = None

        client = _find_client_for_key(response, self._public_key_b64)
        if _is_verified(client):
            # Already paired — skip the toggle prompt entirely.
            return await self.async_step_credentials()

        # Either absent, or present-but-unverified (the gateway only honours the
        # toggle for ~2 minutes after install). Reinstall to get a fresh window.
        try:
            await self._site["api"].add_authorized_client(
                self._public_key_der,
                description="Powerwall Local (Fleet)",
                key_type=AuthorizedClientKeyType.RSA,
                authorized_client_type=AuthorizedClientType.CUSTOMER_MOBILE_APP,
            )
        except TeslaFleetError as err:
            LOGGER.error(
                "add_authorized_client failed for site %s: %s", s.id, err
            )
            return self.async_abort(reason="pair_install_failed")

        return await self.async_step_pair()

    @staticmethod
    def _site_label(s: TeslaFleetEnergyData) -> str:
        name = s.device.get("name") if s.device else None
        return name or f"Energy Site {s.id}"

    async def _site_meta(self, s: TeslaFleetEnergyData) -> dict[str, Any]:
        host = ""
        try:
            host = _extract_host(await s.api.get_networking_status())
        except TeslaFleetError as err:
            LOGGER.warning("Networking status unavailable for site %s: %s", s.id, err)
        return {
            "site_id": s.id,
            "site_name": self._site_label(s),
            "host": host,
            "password": "",
            "api": s.api,
        }

    # ----- key management --------------------------------------------------------

    async def _ensure_key_loaded(self) -> ConfigFlowResult | None:
        """Generate/load the RSA key file once and stash its public form."""
        if self._key_pem is not None:
            return None

        # Key generation is purely local (RSA keygen + file IO); the Tesla base
        # class carries these helpers and needs no session or access token.
        keyholder = Tesla()
        try:
            await keyholder.get_rsa_private_key(self.hass.config.path(KEY_FILENAME))
            self._key_pem = await self.hass.async_add_executor_job(
                Path(self.hass.config.path(KEY_FILENAME)).read_bytes
            )
        except OSError as err:
            LOGGER.error("Could not read/write RSA key: %s", err)
            return self.async_abort(reason="unknown")

        self._public_key_b64 = keyholder.rsa_public_der_pkcs1_b64
        self._public_key_der = keyholder.rsa_public_der_pkcs1
        return None

    # ----- credentials + pair + LAN verify (per site) ----------------------------

    async def async_step_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Prompt the user to toggle the gateway switch, then poll for VERIFIED."""
        assert self._site is not None
        site = self._site

        if user_input is None:
            return self.async_show_form(
                step_id="pair",
                data_schema=vol.Schema({}),
                description_placeholders={"site_name": site["site_name"]},
            )

        for _ in range(KEY_PAIRING_POLL_ATTEMPTS):
            try:
                response = await site["api"].list_authorized_clients()
            except TeslaFleetError:
                response = None
            if _is_verified(_find_client_for_key(response, self._public_key_b64)):
                return await self.async_step_credentials()
            await asyncio.sleep(KEY_PAIRING_POLL_INTERVAL)

        return self.async_show_form(
            step_id="pair",
            data_schema=vol.Schema({}),
            errors={"base": "pair_pending"},
            description_placeholders={"site_name": site["site_name"]},
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect host + password and confirm the LAN connection works."""
        assert self._site is not None
        site = self._site

        errors: dict[str, str] = {}
        if user_input is not None:
            password = (user_input.get(CONF_GATEWAY_PASSWORD) or "").strip()[-5:]
            host = (user_input.get(CONF_GATEWAY_HOST) or "").strip()
            if not password:
                errors["base"] = "invalid_password"
            elif not host:
                errors["base"] = "host_required"
            else:
                site["password"] = password
                site["host"] = host

                from aiopowerwall import (  # noqa: PLC0415
                    PowerwallAuthenticationError,
                    PowerwallClient,
                    PowerwallConnectionError,
                )

                assert self._key_pem is not None
                session = async_get_clientsession(self.hass)
                try:
                    async with PowerwallClient(
                        host=host,
                        gateway_password=password,
                        rsa_private_key_pem=self._key_pem,
                        session=session,
                    ) as client:
                        await client.connect()
                    assert self._parent_entry is not None
                    return self.async_create_entry(
                        title=site["site_name"],
                        data={
                            CONF_PARENT_ENTRY_ID: self._parent_entry.entry_id,
                            CONF_ENERGY_SITE_ID: site["site_id"],
                            CONF_GATEWAY_HOST: host,
                            CONF_GATEWAY_PASSWORD: password,
                            "site_name": site["site_name"],
                        },
                    )
                except PowerwallAuthenticationError:
                    errors["base"] = "invalid_password"
                except PowerwallConnectionError as err:
                    LOGGER.warning(
                        "LAN verify site %s: %s failed: %s",
                        site["site_id"],
                        host,
                        err,
                    )
                    errors["base"] = "cannot_connect_local"
                except Exception as err:  # noqa: BLE001
                    LOGGER.exception("Unexpected LAN verify error: %s", err)
                    errors["base"] = "unknown"

        return self.async_show_form(
            step_id="credentials",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_GATEWAY_HOST, default=site["host"]): str,
                    vol.Required(CONF_GATEWAY_PASSWORD, default=site["password"]): str,
                }
            ),
            errors=errors,
            description_placeholders={"site_name": site["site_name"]},
        )

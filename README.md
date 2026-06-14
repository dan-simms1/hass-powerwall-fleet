# Tesla Powerwall Local (Fleet) — `hass-powerwall-fleet`

Works alongside the **stock Home Assistant [Tesla Fleet](https://www.home-assistant.io/integrations/tesla_fleet/)
integration** — no changes to Tesla Fleet are required — to surface **local
diagnostic information** from your Powerwall that the standard integrations don't
expose. It polls the gateway directly over your LAN, including **per-PV-string
voltage, current and state** (strings A–F), per-Powerwall BMS energy, PCH/inverter
signals, islanding diagnostics and SYNC meter detail. **No cloud round-trips at
runtime** — the Fleet API is used only at setup time to discover the gateway and
register a signed local-access key.

> ## ⚠️ Credits — this is almost entirely Teslemetry's work
>
> This is a near-verbatim fork of
> **[Teslemetry/hass-powerwall-v1r](https://github.com/Teslemetry/hass-powerwall-v1r)**
> by **Teslemetry**. Essentially all of the work — the local TEDAPI protocol research,
> the coordinators, and every single entity — is theirs. **The only change in this
> fork** is re-pointing the one-time setup/pairing flow from the Teslemetry
> integration to the **stock Tesla Fleet integration**; the runtime is byte-for-byte
> the original. Full credit and thanks to Teslemetry — please use and support the
> [original project](https://github.com/Teslemetry/hass-powerwall-v1r).

## Requirements

- Home Assistant 2026.4 or newer
- The **Tesla Fleet** integration installed and loaded, with at least one energy site
- Network reachability from Home Assistant to the Powerwall gateway
- The Powerwall's IP address, and its password — printed **inside the Powerwall
  unit** (behind the front cover) or available from Tesla support (only the last
  5 characters are used)
- Physical access to the Powerwall unit during setup (to flip its disconnect switch)

## Installation (HACS)

1. In HACS, open **Integrations** → menu → **Custom repositories**.
2. Add this repository's URL with category **Integration**.
3. Install **Tesla Powerwall Local (Fleet)** and restart Home Assistant.
4. **Settings → Devices & Services → Add Integration → Tesla Powerwall Local (Fleet)**,
   select the Tesla Fleet entry and energy site, then complete local gateway pairing.

## Manual installation

Copy `custom_components/powerwall_fleet/` into your Home Assistant
`config/custom_components/` directory and restart.

## How pairing works

1. The flow reads your energy sites from the loaded **Tesla Fleet** config entry
   and discovers the gateway's LAN IP via the Fleet `networking_status` endpoint.
2. It generates a local RSA key (stored as `powerwall_fleet.key` in your HA config
   dir) and registers the public key as an *authorized client* via the Fleet
   `add_authorized_client` endpoint.
3. **Confirm at the Powerwall (physically).** You must be standing at the
   **Powerwall 3 unit itself — _not_ the Backup Gateway**. On the **left-hand side**
   of the unit, flip the disconnect switch **off, then back on**, and click
   **Submit**. This proves physical access and authorises the key. *(On a Powerwall 2
   the switch is on the right-hand side.)* If it reports the key isn't confirmed yet,
   flip the switch off/on again and resubmit.
4. **Enter the connection details.** Type the Powerwall's **IP address** and
   **password**. The password is printed **inside the Powerwall unit** (behind the
   front cover) — or obtain it from Tesla support. Enter it **exactly as printed**;
   only its last 5 characters are used (and only those 5 are stored).

After that, every poll is a signed call to the gateway over your LAN — no cloud.

## How it works at runtime

Each gateway endpoint is polled on its own cadence by a dedicated coordinator,
all sharing a single authenticated local `PowerwallClient` (no cloud calls):

- **status** — `/api/system_status`
- **meters** — `/api/meters/aggregates`
- **battery SoE**, **grid status**, **config**, **backup events**
- **components** — TEDAPI component signals (BMS, PCH, **PV strings**, aggregator)

## Coexisting with the Tesla Fleet integration

This integration runs **alongside** the cloud Tesla Fleet integration, which models
the same energy site. To avoid entity_id collisions (both would otherwise want
`sensor.<site>_battery_power` etc.), every device here carries a **"Local"** suffix —
`<Site> Local`, `Powerwall Local`, `Powerwall expansion N Local`. Home Assistant's
normal entity_id generation therefore always includes `local`, e.g.
`select.<site>_local_operation_mode`, `sensor.<site>_local_battery_power`,
`sensor.powerwall_local_pv_string_a_voltage` (plus whatever area/room prefix HA
adds). Entity_ids aren't forced — HA names them naturally — and Tesla Fleet's own
entities are left untouched, so the local copy sits next to the cloud one with no
clashes.

## Entities

The integration creates one device per energy site, plus per-Powerwall and
per-expansion battery devices. This fork enables almost all sensors by default
(upstream disabled many useful ones, including the PV strings). The only group left
off by default is the redundant per-CT **SYNC-meter** diagnostics (24 entities) — turn
those on from the device page if you want them; disable anything you don't.

### PV strings (the headline feature)
Per string A–F: **voltage**, **current**, **state**, plus a **PV power** sum.

### Power flows
- Battery / Site / Load / Solar power, plus Solar RGM, Generator, Conductor

### Battery
- State of energy, percentage charged, energy remaining, full pack energy
- Per-Powerwall BMS energy remaining / full pack / charge

### PCH / inverter, meters, islanding, grid & config
- PCH AC frequency/voltage/power, AC mode, DC-DC state
- Per-location meter aggregates; SYNC meters X / Y per-CT detail
- Island mode, islander grid state, per-phase frequency & voltage
- Grid status, backup reserve, net meter mode, export rule, nominal energy/power

See `custom_components/powerwall_fleet/strings.json` for the full entity list.

## Credits

This project is a derivative work of
**[Teslemetry/hass-powerwall-v1r](https://github.com/Teslemetry/hass-powerwall-v1r)**
(© Teslemetry), used under the Apache License 2.0. The local protocol work builds
on [`aiopowerwall`](https://github.com/Teslemetry/aiopowerwall) and
[`tesla-fleet-api`](https://github.com/Teslemetry/python-tesla-fleet-api). The only
substantive change here is re-pointing the config flow from the Teslemetry
integration to the Home Assistant **Tesla Fleet** integration.

The upstream repository is retained as the `upstream` git remote so future fixes
can be pulled in.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

Maintained by [@dan-simms1](https://github.com/dan-simms1) at
<https://github.com/dan-simms1/hass-powerwall-fleet>.

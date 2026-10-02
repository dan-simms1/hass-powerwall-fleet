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

Each gateway endpoint has its own coordinator and its own cadence, all sharing a
single authenticated local `PowerwallClient` (no cloud calls):

| Coordinator | Endpoint | Interval | Default |
|---|---|---|---|
| **battery SoE** | `/api/system_status/soe` | 30s | on |
| **status** | `/api/system_status` | 60s | on |
| **grid status** | `/api/system_status/grid_status` | 300s | on |
| **config** | gateway `config.json` | 300s | on |
| **meters** | `/api/meters/aggregates` | 300s | **off*** |
| **components** | TEDAPI signals (BMS, PCH, **PV strings**, aggregator) | 300s | **off*** |
| **backup events** | manual backup events | 300s | **off*** |

\* off for new installs; existing installs keep them on across the upgrade.

The gateway is usually a Wi-Fi client in power-save mode, where what costs it is
waking the radio, not moving bytes — and on a typical install, connection setup
was about 95% of the traffic. So:

- **Connections are held open between polls.** Home Assistant's shared session
  leaves aiohttp's ~15s keep-alive in place, which is shorter than every poll gap
  here, so each poll used to pay a fresh TCP + TLS handshake. The integration uses
  its own connector with a 300s keep-alive, capped at one connection per host.
- **Polls are aligned.** Every interval is a whole multiple of the shortest one,
  and a single tick drives them all, so the coordinators fall due together in one
  burst instead of waking the radio at unrelated moments.
- **Unused endpoint groups are off for new installs.** The three optional ones above
  back entities most installs never read, and they were the bulk of the request load.
  Upgrading never takes them away: an existing config entry keeps all three on, so
  nothing you already had disappears — switch them off yourself to claim the saving.
  Toggle any of them from the integration's *Configure* options; entities return with
  their original ids, so history is preserved either way.
- **Nothing is polled that nothing reads.** An endpoint whose entities are all
  disabled in the entity registry isn't polled at all, and Home Assistant's
  *Enable polling for updates* switch turns the lot off.

Together these take a default install from ~1,206 requests/hour, each on its own
fresh connection, to ~194 requests/hour over a handful of persistent ones — or
~603/hour down to ~97/hour on the **Relaxed** profile. Measured on a real gateway:
no new connections at all across a 180s capture, and 5.9 MB/hour down to 0.46.

## Maintenance & options

- **Re-authentication** — if the gateway ever rejects the stored key/password (after a
  firmware update, key revocation, or password change), Home Assistant prompts you to
  re-pair (re-register the key + toggle the unit switch) without losing your history.
- **Reconfigure** — change the Powerwall's **IP address or password** in place (e.g.
  after a DHCP change) via the integration's *Reconfigure* option; no need to delete it.
- **Polling profile** (integration *Configure* / options) — **Fast** halves every poll
  interval (fresher data, more LAN traffic), **Relaxed** doubles them; **Normal** is the
  default. The same options page switches the three optional endpoint groups
  (meter aggregates, Powerwall 3 unit telemetry, manual backup events) back on.
- **State precision** — the gateway reports raw floats (a state of charge comes back as
  `97.027972027972`), which would make almost every sensor "change" on every poll and
  flood websocket clients with `state_changed` events — enough to knock a dashboard
  tablet offline. States are therefore rounded at source to the precision each
  measurement actually carries: 10 W for power, 0.1 V, 0.01 Hz, 0.01 A, 0.1 % for state
  of charge, 0.1 h for backup time, and 1 Wh for energy counters (fine enough that
  long-term statistics and Energy dashboard totals are unaffected). The table lives in
  `precision.py` if you want to retune it.

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
(upstream disabled many useful ones, including the PV strings). The redundant per-CT
**SYNC-meter** diagnostics (24 entities) are left off in the entity registry — turn
those on from the device page if you want them; disable anything you don't.

The **PV strings**, per-location **meter aggregates** and **manual backup** entities
belong to the optional endpoint groups above, so they only appear once you switch
their group on in the integration's options.

### PV strings (the headline feature)
Per string A–F: **voltage**, **current**, **power** (derived V × I), **state**, plus a
total **PV power** sum.

### Backup & gateway
- **Backup time remaining** (estimated runtime at the current load)
- **Gateway time** (diagnostic)
- The device page links to the Powerwall's local web UI (`configuration_url`)

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

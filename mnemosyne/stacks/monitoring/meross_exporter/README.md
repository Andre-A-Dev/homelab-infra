# meross_exporter

Prometheus exporter for Meross smart plugs. Polls the Meross cloud API and
exposes per-device electricity metrics. Part of the monitoring stack —
built by `docker compose` and scraped by Prometheus at `meross-exporter:9114`.

## Metrics

| Metric | Labels | Description |
|---|---|---|
| `meross_device_online` | `account`, `device` | 1 = reachable, 0 = offline |
| `meross_switch_state` | `account`, `device`, `channel` | 1 = on, 0 = off |
| `meross_power_watts` | `account`, `device` | Instantaneous power draw |
| `meross_voltage_volts` | `account`, `device` | Voltage |
| `meross_current_amps` | `account`, `device` | Current draw |
| `meross_energy_today_kwh` | `account`, `device` | Energy consumed today (optional) |

`meross_energy_today_kwh` is only emitted by devices that implement
`ConsumptionXMixin` — not all Meross plug models expose it.

## Configuration

Set in the monitoring stack's `.env` (see `.env.example`):

| Variable | Default | Description |
|---|---|---|
| `MEROSS_EMAIL_1` | — | Account 1 email |
| `MEROSS_PASSWORD_1` | — | Account 1 password |
| `MEROSS_ALIAS_1` | — | Short label used in metric labels for account 1 |
| `MEROSS_EMAIL_2` | — | Account 2 (optional) |
| `MEROSS_PASSWORD_2` | — | Account 2 password |
| `MEROSS_ALIAS_2` | — | Short label for account 2 |
| `MEROSS_DEVICE_NAMES` | _(all)_ | Comma-separated device name filter; empty = all devices |
| `MEROSS_API_URL` | `https://iotx-eu.meross.com` | EU API endpoint |
| `POLL_INTERVAL` | `15` | Seconds between polls |
| `RECONNECT_INTERVAL` | `60` | Seconds before reconnect after a connection error |
| `EXPORTER_PORT` | `9114` | HTTP metrics port |

Single-account fallback: if no numbered vars are set, `MEROSS_EMAIL` /
`MEROSS_PASSWORD` / `MEROSS_ALIAS` are used instead.

## Multiple accounts

Each account runs as a concurrent asyncio task with its own reconnect loop —
one account's failure does not affect the other. The `account` label in every
metric is set to `MEROSS_ALIAS_N` (defaults to the email prefix if not set).

## Key constraints

- **Meross has regional API endpoints.** `iotx-eu.meross.com` is for EU
  accounts. Devices registered to a non-EU Meross account will fail to connect.
  Check the Meross app account settings for the correct region.
- **Library version is pinned** (`meross-iot==0.4.7.4`). The Meross IoT
  library's API changes frequently between minor versions. Do not upgrade
  without testing — the `async_get_instant_metrics` / `async_get_electricity`
  dual call and the `_is_on` version-safe wrapper exist because of past breakage.
- **Healthcheck is alias-specific.** The compose healthcheck looks for
  `meross_device_online.*account="Home"` — it expects the first account alias
  to be `Home`. Update the healthcheck if the alias changes.
- **Electricity metrics require `ElectricityMixin`.** Basic Meross plugs without
  energy monitoring will appear in `meross_switch_state` but not in power/voltage/current metrics.

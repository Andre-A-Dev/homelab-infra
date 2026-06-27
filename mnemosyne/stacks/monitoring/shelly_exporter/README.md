# Shelly Prometheus Exporter

Polls Shelly devices via local HTTP — no cloud, no MQTT.

## Supported devices

| Type | Config var | Gen |
|---|---|---|
| Power plug | `SHELLY_DEVICES` | Gen1, Gen2/3 |
| H&T sensor | `SHELLY_HT_DEVICES` | Gen3 (direct WiFi) |
| BLU H&T sensor | `SHELLY_BLU_DEVICES` | BLU (via gateway KVS) |

## Configuration

All variables go into the stack `.env` file.

### Power plugs — `SHELLY_DEVICES`
Format: `name:host:gen` (comma-separated, `gen` = `1` or `2`)
```
SHELLY_DEVICES=PC-Desk:192.168.1.12:2,NAS:192.168.1.x:1
```

### H&T Gen3 — `SHELLY_HT_DEVICES`
Format: `name;host` (semicolon separator, comma between devices)
```
SHELLY_HT_DEVICES=HT-Keller;192.168.1.x
```

### BLU H&T via gateway — `SHELLY_BLU_DEVICES`
Format: `name;gateway_host;mac` (MAC address contains colons, hence semicolon separator)
```
SHELLY_BLU_DEVICES=BLU-Keller;192.168.1.x;c0:2c:ed:77:df:12
```

### Other settings
```
POLL_INTERVAL_SHELLY=15   # seconds between polls
SHELLY_TIMEOUT=5          # HTTP request timeout in seconds
```

## BLU Gateway setup

The BLU H&T device broadcasts via BLE. A Gen2/3 gateway device running
`blu_gateway.js` picks up the advertisement and writes the data into its KVS.
The exporter then reads the KVS via `GET /rpc/KVS.Get?key=<mac>`.

**Steps (one-time, on the gateway device):**

1. `http://<gateway-ip>` → Settings → Bluetooth → enable
2. Scripts → Add script → paste contents of `blu_gateway.js` → Save
3. Enable **Auto-start on boot**, then click **Run**
4. Verify: press the small button on the BLU device to force an advertisement,
   then check the script log for a line like:
   `BLU-HT {"temperature":21.3,"humidity":48.2,"battery":91,...}`
5. Confirm KVS entry:
   ```
   curl "http://<gateway-ip>/rpc/KVS.Get?key=c0:2c:ed:77:df:12"
   ```

To add more BLU devices, extend `CONFIG.devices` in `blu_gateway.js` and add
a corresponding entry to `SHELLY_BLU_DEVICES`.

## Exposed metrics

| Metric | Unit | Devices |
|---|---|---|
| `shelly_device_online` | 0/1 | all |
| `shelly_power_watts` | W | plugs |
| `shelly_voltage_volts` | V | plugs Gen2/3 |
| `shelly_current_amps` | A | plugs Gen2/3 |
| `shelly_energy_total_kwh` | kWh | plugs |
| `shelly_switch_state` | 0/1 | plugs |
| `shelly_device_temp_celsius` | °C | plugs (internal) |
| `shelly_temperature_celsius` | °C | H&T, BLU H&T |
| `shelly_humidity_percent` | % | H&T, BLU H&T |
| `shelly_battery_percent` | % | H&T, BLU H&T |

All metrics carry labels `device` (display name) and `host` (IP address).

## Rebuild & restart

```bash
docker compose up -d --build shelly-exporter
```

> `restart` alone does not reload `.env` or rebuild the image.

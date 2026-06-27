/**
 * BLU H&T Gateway Script
 *
 * Listens for BLE advertisements from Shelly BLU H&T devices and stores
 * temperature, humidity and battery in the gateway's KVS under the device
 * MAC address as key.
 *
 * The Prometheus exporter reads: GET /rpc/KVS.Get?key=<mac>
 *
 * Setup:
 *   1. Enable Bluetooth on the gateway device (Settings → Bluetooth → enable)
 *   2. Paste this script into Scripts → Add script → Run
 *   3. Enable "Auto-start on boot"
 *
 * Add one entry per BLU H&T device to CONFIG.devices.
 */

const CONFIG = {
    devices: [
        { addr: "c0:2c:ed:77:df:12", name: "BLU-HT" }
    ]
};

// --- BTHome decoder ----------------------------------------------------------

const BTHOME_SVC_ID = "fcd2";

const uint8  = 0;
const int8   = 1;
const uint16 = 2;
const int16  = 3;
const uint24 = 4;
const int24  = 5;

const BTH = {
    0x00: { n: "pid",         t: uint8  },
    0x01: { n: "battery",     t: uint8  },
    0x02: { n: "temperature", t: int16,  f: 0.01 },
    0x03: { n: "humidity",    t: uint16, f: 0.01 },
    0x2e: { n: "humidity",    t: uint8           },
    0x45: { n: "temperature", t: int16,  f: 0.1  },
};

function byteSize(type) {
    if (type === uint8  || type === int8)  return 1;
    if (type === uint16 || type === int16) return 2;
    if (type === uint24 || type === int24) return 3;
    return 255;
}

const Dec = {
    utoi: function(n, bits) {
        const mask = 1 << (bits - 1);
        return n & mask ? n - (1 << bits) : n;
    },
    u8:  function(b) { return b.at(0); },
    i8:  function(b) { return this.utoi(b.at(0), 8); },
    u16: function(b) { return 0xffff & ((b.at(1) << 8) | b.at(0)); },
    i16: function(b) { return this.utoi(this.u16(b), 16); },
    u24: function(b) { return 0xffffff & ((b.at(2) << 16) | (b.at(1) << 8) | b.at(0)); },
    val: function(type, b) {
        if (b.length < byteSize(type)) return null;
        if (type === uint8)  return this.u8(b);
        if (type === int8)   return this.i8(b);
        if (type === uint16) return this.u16(b);
        if (type === int16)  return this.i16(b);
        if (type === uint24) return this.u24(b);
        if (type === int24)  return this.utoi(this.u24(b), 24);
        return null;
    },
    unpack: function(buf) {
        if (typeof buf !== "string" || buf.length === 0) return null;
        let out = {};
        let dib = buf.at(0);
        out.encryption      = (dib & 0x1) !== 0;
        out.BTHome_version  = dib >> 5;
        if (out.BTHome_version !== 2 || out.encryption) return out;
        buf = buf.slice(1);
        while (buf.length > 0) {
            let def = BTH[buf.at(0)];
            if (typeof def === "undefined") break;
            buf = buf.slice(1);
            let v = this.val(def.t, buf);
            if (v === null) break;
            if (typeof def.f !== "undefined") v = v * def.f;
            if (typeof out[def.n] === "undefined") out[def.n] = v;
            buf = buf.slice(byteSize(def.t));
        }
        return out;
    }
};

// --- BLE scan ----------------------------------------------------------------

let lastPid = {};

function round1(v) { return Math.round(v * 10) / 10; }

function onScan(event, result) {
    if (event !== BLE.Scanner.SCAN_RESULT) return;

    let device = null;
    for (let i = 0; i < CONFIG.devices.length; i++) {
        if (CONFIG.devices[i].addr === result.addr) {
            device = CONFIG.devices[i];
            break;
        }
    }
    if (device === null) return;

    let svc = result.service_data;
    if (!svc || typeof svc[BTHOME_SVC_ID] === "undefined") return;

    let data = Dec.unpack(svc[BTHOME_SVC_ID]);
    if (!data || data.encryption) return;

    if (lastPid[device.addr] === data.pid) return;
    lastPid[device.addr] = data.pid;

    let payload = JSON.stringify({
        temperature: typeof data.temperature !== "undefined" ? round1(data.temperature) : null,
        humidity:    typeof data.humidity    !== "undefined" ? round1(data.humidity)    : null,
        battery:     typeof data.battery     !== "undefined" ? data.battery             : null,
        rssi:        result.rssi,
        ts:          Math.floor(Date.now() / 1000)
    });

    Shelly.call("KVS.Set", { key: device.addr, value: payload }, function(_, err) {
        if (err !== 0) {
            console.log("KVS.Set failed for", device.addr, "err:", err);
        } else {
            console.log(device.name, payload);
        }
    });
}

function init() {
    let bleCfg = Shelly.getComponentConfig("ble");
    if (!bleCfg || !bleCfg.enable) {
        console.log("Error: Bluetooth disabled — enable it under Settings > Bluetooth");
        return;
    }
    if (!BLE.Scanner.isRunning()) {
        let ok = BLE.Scanner.Start({ duration_ms: -1, active: false });
        if (!ok) {
            console.log("Error: could not start BLE scanner");
            return;
        }
    }
    BLE.Scanner.Subscribe(onScan);
    console.log("BLU H&T Gateway ready, watching", CONFIG.devices.length, "device(s)");
}

init();

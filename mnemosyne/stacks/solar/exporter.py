import time
import requests
import urllib3
from prometheus_client import start_http_server, Gauge
import os

urllib3.disable_warnings()

USERNAME = os.environ.get("SOLAR_USERNAME")
PASSWORD = os.environ.get("SOLAR_PASSWORD")
BASE_URL  = os.environ.get("SOLAR_BASE_URL")

if not all([USERNAME, PASSWORD, BASE_URL]):
    raise ValueError("SOLAR_USERNAME, SOLAR_PASSWORD and SOLAR_BASE_URL must be set")

METRICS = {
    "radiationIntensity":       Gauge("solar_radiation_intensity",        "Strahlungsintensität (W/m²)"),
    "inverterPower":            Gauge("solar_inverter_power_w",           "Aktuelle PV-Leistung (W)"),
    "dailyEnergy":              Gauge("solar_daily_energy_kwh",           "Tagesertrag (kWh)"),
    "totalEnergy":              Gauge("solar_total_energy_kwh",           "Gesamtertrag (kWh)"),
    "monthEnergy":              Gauge("solar_month_energy_kwh",           "Monatsertrag (kWh)"),
    "batterySoc":               Gauge("solar_battery_soc_percent",        "Batterie Ladestand (%)"),
    "chargePower":              Gauge("solar_battery_charge_power_w",     "Batterie Ladeleistung (W)"),
    "disChargePower":           Gauge("solar_battery_discharge_power_w",  "Batterie Entladeleistung (W)"),
    "gridPower":                Gauge("solar_grid_power_w",               "Netz Einspeisung/Bezug (W)"),
    "usePower":                 Gauge("solar_load_power_w",               "Hausverbrauch (W)"),
    "selfUsePower":             Gauge("solar_self_use_power_w",           "Eigenverbrauch (W)"),
}

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"})

def login():
    resp = session.post(
        f"{BASE_URL}/rest/plat/smapp/v1/oauth/token",
        json={
            "organizationName": "",
            "userName": USERNAME,
            "password": PASSWORD
        },
        verify=False, timeout=10
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != 0:
        raise Exception(f"Login fehlgeschlagen: {data}")
    token = data["data"]["accessToken"]
    session.headers.update({"XSRF-TOKEN": token})
    print(f"Login erfolgreich")

def get_station_list():
    resp = session.post(
        f"{BASE_URL}/rest/pvms/web/station/v1/overview/queryStationList",
        json={"curPage": 1, "pageSize": 10},
        verify=False, timeout=10
    )
    data = resp.json()
    if data.get("status") != 0:
        raise Exception(f"Station-Abfrage fehlgeschlagen: {data}")
    return data.get("data", {}).get("list", [])

def get_station_kpi(station_dn):
    resp = session.post(
        f"{BASE_URL}/rest/pvms/web/station/v1/overview/queryStationRealKpi",
        json={"stationDn": station_dn},
        verify=False, timeout=10
    )
    data = resp.json()
    if data.get("status") != 0:
        return None
    return data.get("data", {})

def collect():
    try:
        stations = get_station_list()
        if not stations:
            print("Keine Anlagen gefunden")
            return

        for station in stations:
            kpi = get_station_kpi(station["dn"])
            if not kpi:
                continue
            for key, gauge in METRICS.items():
                if key in kpi and kpi[key] is not None:
                    try:
                        gauge.set(float(kpi[key]))
                    except (ValueError, TypeError):
                        pass

        print(f"Metriken aktualisiert: {time.strftime('%H:%M:%S')}")

    except Exception as e:
        print(f"Fehler beim Sammeln: {e}")
        print("Versuche Re-Login...")
        try:
            login()
        except Exception as le:
            print(f"Re-Login fehlgeschlagen: {le}")

if __name__ == "__main__":
    start_http_server(9401)
    print("Exporter gestartet auf :9401")
    login()
    while True:
        collect()
        time.sleep(300)

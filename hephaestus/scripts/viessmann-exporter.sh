#!/bin/bash
# Viessmann vcontrold → Prometheus textfile collector exporter
# Queries Vitotronic 200 KW2 via vclient and writes metrics for node-exporter

TEXTFILE="/var/lib/node_exporter/textfile_collector/viessmann.prom"
TMPFILE="${TEXTFILE}.tmp"
VCLIENT="/usr/local/bin/vclient"
VHOST="127.0.0.1:3002"

# Query all metrics in a single call to minimize serial communication time
OUTPUT=$($VCLIENT -h "$VHOST" -c \
  getTempA,getTempWWist,getTempWWsoll,getTempKist,getTempKsoll,\
getBrennerStatus,getBrennerStarts,getBrennerStunden1,\
getPumpeStatusM1,getPumpeStatusSp,getPumpeStatusZirku,\
getBetriebArtM1,getStatusStoerung,getNeigungM1,getNiveauM1,\
getTempRaumNorSollM1 2>/dev/null)

# Exit if vclient returned nothing (vcontrold unreachable)
if [ -z "$OUTPUT" ]; then
  echo "# viessmann-exporter: vclient returned no output" > "$TMPFILE"
  mv "$TMPFILE" "$TEXTFILE"
  exit 1
fi

# Helper: extract numeric value for a given command name
get_value() {
  echo "$OUTPUT" | grep -A1 "^${1}:" | tail -1 | awk '{print $1}'
}

# Helper: map BetriebsArt enum to numeric value for Prometheus
# WW=0, RED=1, NORM=2, H+WW=3, ABSCHALT=4
betriebsart_to_num() {
  case "$1" in
    WW)       echo 0 ;;
    RED)      echo 1 ;;
    NORM)     echo 2 ;;
    H+WW)     echo 3 ;;
    ABSCHALT) echo 4 ;;
    *)        echo -1 ;;
  esac
}

TEMP_A=$(get_value "getTempA")
TEMP_WW_IST=$(get_value "getTempWWist")
TEMP_WW_SOLL=$(get_value "getTempWWsoll")
TEMP_K_IST=$(get_value "getTempKist")
TEMP_K_SOLL=$(get_value "getTempKsoll")
BRENNER_STATUS=$(get_value "getBrennerStatus")
BRENNER_STARTS=$(get_value "getBrennerStarts")
BRENNER_STUNDEN=$(get_value "getBrennerStunden1")
PUMPE_M1=$(get_value "getPumpeStatusM1")
PUMPE_SP=$(get_value "getPumpeStatusSp")
PUMPE_ZIRKU=$(get_value "getPumpeStatusZirku")
BETRIEB_RAW=$(echo "$OUTPUT" | grep -A1 "^getBetriebArtM1:" | tail -1 | awk '{print $1}')
BETRIEB_M1=$(betriebsart_to_num "$BETRIEB_RAW")
STOERUNG=$(get_value "getStatusStoerung")
NEIGUNG_M1=$(get_value "getNeigungM1")
NIVEAU_M1=$(get_value "getNiveauM1")
RT_SOLL=$(get_value "getTempRaumNorSollM1")

# Fallback if RT_SOLL read fails
RT_SOLL=${RT_SOLL:-20}

# Write metrics to tmpfile, then atomically move to final location
# Atomic move prevents node-exporter from reading a partial file
cat > "$TMPFILE" << METRICS
# HELP viessmann_temperature_outdoor_celsius Outdoor temperature in degrees Celsius
# TYPE viessmann_temperature_outdoor_celsius gauge
viessmann_temperature_outdoor_celsius $TEMP_A

# HELP viessmann_temperature_boiler_actual_celsius Actual boiler temperature in degrees Celsius
# TYPE viessmann_temperature_boiler_actual_celsius gauge
viessmann_temperature_boiler_actual_celsius $TEMP_K_IST

# HELP viessmann_temperature_boiler_target_celsius Target boiler temperature in degrees Celsius
# TYPE viessmann_temperature_boiler_target_celsius gauge
viessmann_temperature_boiler_target_celsius $TEMP_K_SOLL

# HELP viessmann_temperature_hotwater_actual_celsius Actual hot water temperature in degrees Celsius
# TYPE viessmann_temperature_hotwater_actual_celsius gauge
viessmann_temperature_hotwater_actual_celsius $TEMP_WW_IST

# HELP viessmann_temperature_hotwater_target_celsius Target hot water temperature in degrees Celsius
# TYPE viessmann_temperature_hotwater_target_celsius gauge
viessmann_temperature_hotwater_target_celsius $TEMP_WW_SOLL

# HELP viessmann_burner_active Burner status (1=active, 0=inactive)
# TYPE viessmann_burner_active gauge
viessmann_burner_active $BRENNER_STATUS

# HELP viessmann_burner_starts_total Total number of burner starts
# TYPE viessmann_burner_starts_total counter
viessmann_burner_starts_total $BRENNER_STARTS

# HELP viessmann_burner_hours_total Total burner operating hours
# TYPE viessmann_burner_hours_total counter
viessmann_burner_hours_total $BRENNER_STUNDEN

# HELP viessmann_pump_heating_active Heating circuit pump M1 status (1=active, 0=inactive)
# TYPE viessmann_pump_heating_active gauge
viessmann_pump_heating_active $PUMPE_M1

# HELP viessmann_pump_storage_active Storage loading pump status (1=active, 0=inactive)
# TYPE viessmann_pump_storage_active gauge
viessmann_pump_storage_active $PUMPE_SP

# HELP viessmann_pump_circulation_active Circulation pump status (1=active, 0=inactive)
# TYPE viessmann_pump_circulation_active gauge
viessmann_pump_circulation_active $PUMPE_ZIRKU

# HELP viessmann_operating_mode_m1 Operating mode M1 (0=WW, 1=RED, 2=NORM, 3=H+WW, 4=ABSCHALT)
# TYPE viessmann_operating_mode_m1 gauge
viessmann_operating_mode_m1 $BETRIEB_M1

# HELP viessmann_fault_active Collective fault status (1=fault, 0=ok)
# TYPE viessmann_fault_active gauge
viessmann_fault_active $STOERUNG

# HELP viessmann_heating_curve_slope_m1 Heating curve slope M1
# TYPE viessmann_heating_curve_slope_m1 gauge
viessmann_heating_curve_slope_m1 $NEIGUNG_M1

# HELP viessmann_heating_curve_level_m1 Heating curve level M1
# TYPE viessmann_heating_curve_level_m1 gauge
viessmann_heating_curve_level_m1 $NIVEAU_M1

# HELP viessmann_room_target_normal_celsius Room target temperature normal M1
# TYPE viessmann_room_target_normal_celsius gauge
viessmann_room_target_normal_celsius $RT_SOLL

# HELP viessmann_last_update_timestamp Unix timestamp of last successful update
# TYPE viessmann_last_update_timestamp gauge
viessmann_last_update_timestamp $(date +%s)
METRICS

# ── VT Soll current: calculated target flow temp at actual outdoor temperature ─
# Uses live RT_SOLL from controller instead of hardcoded value
DAR_NOW=$(echo "$TEMP_A - $RT_SOLL" | bc)
VT_SOLL_NOW=$(echo "scale=2; $RT_SOLL + $NIVEAU_M1 - $NEIGUNG_M1 * $DAR_NOW * (1.4347 + 0.021 * $DAR_NOW + 0.0002479 * $DAR_NOW * $DAR_NOW)" | bc)
VT_SOLL_MED_NOW=$(echo "scale=2; $VT_SOLL_NOW + 10" | bc)
VT_SOLL_MAX_NOW=$(echo "scale=2; $VT_SOLL_NOW + 20" | bc)

cat >> "$TMPFILE" << METRICS

# HELP viessmann_vt_soll_current_celsius Calculated target flow temperature at current outdoor temperature (min band)
# TYPE viessmann_vt_soll_current_celsius gauge
viessmann_vt_soll_current_celsius{band="min"} $VT_SOLL_NOW
viessmann_vt_soll_current_celsius{band="med"} $VT_SOLL_MED_NOW
viessmann_vt_soll_current_celsius{band="max"} $VT_SOLL_MAX_NOW
METRICS

# ── VT Soll table: calculated target flow temperature for -20°C to +20°C ─────
# Formula: VT Soll = RT_Soll + Niveau - Neigung * DAR * (1.4347 + 0.021*DAR + 0.0002479*DAR^2)
# DAR = Außentemperatur - RT_Soll | Source: Viessmann community forum
echo "# HELP viessmann_vt_soll_celsius Calculated target flow temperature per outdoor temperature step" >> "$TMPFILE"
echo "# TYPE viessmann_vt_soll_celsius gauge" >> "$TMPFILE"

for AT in 20 19 18 17 16 15 14 13 12 11 10 9 8 7 6 5 4 3 2 1 0 -1 -2 -3 -4 -5 -6 -7 -8 -9 -10 -11 -12 -13 -14 -15 -16 -17 -18 -19 -20; do
    DAR=$(echo "$AT - $RT_SOLL" | bc)
    DAR2=$(echo "$DAR * $DAR" | bc)
    VT_MIN=$(echo "scale=4; $RT_SOLL + $NIVEAU_M1 - $NEIGUNG_M1 * $DAR * (1.4347 + 0.021 * $DAR + 0.0002479 * $DAR * $DAR)" | bc)
    VT_MED=$(echo "scale=4; $VT_MIN + 10" | bc)
    VT_MAX=$(echo "scale=4; $VT_MIN + 20" | bc)
    echo "viessmann_vt_soll_celsius{outdoor_temp_c=\"${AT}\",band=\"min\",dar=\"${DAR}\",dar2=\"${DAR2}\"} $VT_MIN" >> "$TMPFILE"
    echo "viessmann_vt_soll_celsius{outdoor_temp_c=\"${AT}\",band=\"med\",dar=\"${DAR}\",dar2=\"${DAR2}\"} $VT_MED" >> "$TMPFILE"
    echo "viessmann_vt_soll_celsius{outdoor_temp_c=\"${AT}\",band=\"max\",dar=\"${DAR}\",dar2=\"${DAR2}\"} $VT_MAX" >> "$TMPFILE"
done

mv "$TMPFILE" "$TEXTFILE"
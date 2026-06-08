#!/bin/bash
# fan-metrics.sh — Pi 5 fan level + CPU temp → Prometheus textfile collector
# Fan level is 0–4 (pwm-fan driver), not RPM — Pi 5 has no tachometer

TEXTFILE_DIR="/var/lib/node_exporter/textfile_collector"
OUTFILE="${TEXTFILE_DIR}/fan.prom"
TMPFILE="${OUTFILE}.tmp"

THERMAL_BASE="/sys/class/thermal/cooling_device0"

# Read fan level (0–4) and max level
FAN_LEVEL=$(cat "${THERMAL_BASE}/cur_state" 2>/dev/null)
FAN_MAX=$(cat "${THERMAL_BASE}/max_state" 2>/dev/null)

# Read CPU temperature from thermal zone 0 (value in millidegrees)
CPU_TEMP_RAW=$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null)

# Abort if any value is missing
if [[ -z "$FAN_LEVEL" || -z "$FAN_MAX" || -z "$CPU_TEMP_RAW" ]]; then
    echo "fan-metrics: failed to read sensor values" >&2
    exit 1
fi

# Convert millidegrees to degrees (integer division)
CPU_TEMP_C=$(echo "scale=1; ${CPU_TEMP_RAW} / 1000" | bc)

# Write atomically — never leave a partial .prom file for Node Exporter to read
cat > "${TMPFILE}" << EOF
# HELP node_fan_level Pi 5 fan speed level reported by pwm-fan driver (0=off, 4=full)
# TYPE node_fan_level gauge
node_fan_level{instance="mnemosyne"} ${FAN_LEVEL}

# HELP node_fan_level_max Maximum fan level supported by pwm-fan driver
# TYPE node_fan_level_max gauge
node_fan_level_max{instance="mnemosyne"} ${FAN_MAX}

# HELP node_fan_level_ratio Fan level as fraction of maximum (0.0–1.0)
# TYPE node_fan_level_ratio gauge
node_fan_level_ratio{instance="mnemosyne"} $(echo "scale=2; ${FAN_LEVEL} / ${FAN_MAX}" | bc)

# HELP node_cpu_temperature_celsius CPU temperature in degrees Celsius
# TYPE node_cpu_temperature_celsius gauge
node_cpu_temperature_celsius{instance="mnemosyne"} ${CPU_TEMP_C}
EOF

mv "${TMPFILE}" "${OUTFILE}"
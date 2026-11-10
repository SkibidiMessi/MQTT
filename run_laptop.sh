#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ ! -x .venv/bin/python ]]; then
    python3 -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.txt
mkdir -p reports
if [[ -z "${MQTT_PASSWORD:-}" ]]; then
    read -rsp 'Mat khau MQTT: ' MQTT_PASSWORD
    printf '\n'
    export MQTT_PASSWORD
fi
UAV_DEVICE_ID="${1:-uav01}"
if [[ ! "$UAV_DEVICE_ID" =~ ^[a-zA-Z0-9]{1,64}$ ]]; then
    printf 'deviceID chi gom chu va so. Vi du: bash run_laptop.sh uav01\n' >&2
    exit 1
fi
printf 'deviceID=%s\nData: lac/%s/data\nControl: lac/%s/control (chi ghi log)\n' "$UAV_DEVICE_ID" "$UAV_DEVICE_ID" "$UAV_DEVICE_ID"
exec .venv/bin/python -u telemetry_reader.py \
    --connection /dev/serial/by-id/usb-Auterion_PX4_FMU_v6C.x_0-if00 \
    --uav-id "$UAV_DEVICE_ID" --request-streams \
    --mqtt-config mqtt.remote.json --output reports/status.json --quiet

#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$root/scripts/smoke/_dlsps_lib.sh"

dlsps_require_container

echo "Starting CPU pipeline (config/dlsps/cpu/config.json must be the currently mounted config)..." >&2
DLSPS_PIPELINE_NAME=handheld_cpu_detect
iid=$(dlsps_start_pipeline CPU)
echo "Started instance $iid" >&2
trap 'dlsps_stop_pipeline "$iid" || echo "WARN: instance $iid may still be running, check manually" >&2' EXIT

status=$(dlsps_wait_pipeline "$iid" 15 5)
echo "$status"

echo "PASS: DLSPS CPU variant (device=CPU) instance $iid produced real advancing detections."

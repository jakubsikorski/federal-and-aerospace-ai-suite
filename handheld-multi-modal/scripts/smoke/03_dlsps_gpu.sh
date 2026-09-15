#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$root/scripts/smoke/_dlsps_lib.sh"

dlsps_require_container

echo "Starting GPU pipeline (config/dlsps/gpu/config.json must be the currently mounted config)..." >&2
DLSPS_PIPELINE_NAME=handheld_gpu_detect
iid=$(dlsps_start_pipeline GPU)
echo "Started instance $iid" >&2
trap 'dlsps_stop_pipeline "$iid" || echo "WARN: instance $iid may still be running, check manually" >&2' EXIT

status=$(dlsps_wait_pipeline "$iid" 15 5)
echo "$status"

echo "PASS: DLSPS GPU variant (device=GPU, pre-process-backend=va-surface-sharing) instance $iid produced real advancing detections."

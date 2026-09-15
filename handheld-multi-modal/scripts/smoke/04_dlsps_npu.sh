#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$root/scripts/smoke/_dlsps_lib.sh"

dlsps_require_container

echo "Starting NPU pipeline (config/dlsps/npu/config.json — the shipped default — must be the currently mounted config)..." >&2
start_ts=$(date -u +%s)
DLSPS_PIPELINE_NAME=handheld_npu_detect
iid=$(dlsps_start_pipeline NPU)
echo "Started instance $iid" >&2
trap 'dlsps_stop_pipeline "$iid" || echo "WARN: instance $iid may still be running, check manually" >&2' EXIT

status=$(dlsps_wait_pipeline "$iid" 15 5)
echo "$status"

# NPU-specific: gvadetect is requested with device=NPU and pre-process-backend=va
# (see config/dlsps/npu/config.json); OpenVINO must compile the INT8 IR for the
# NPU plugin for the pipeline to even reach RUNNING/produce fps output above —
# a silent CPU/GPU fallback would still show fps, so also grep logs emitted
# since this instance started for an explicit NPU compile/plugin error, which
# OpenVINO raises loudly on failure, not silently.
# NOTE: docker logs --since takes a bare unix timestamp or RFC3339 — the "@"
# prefix is invalid and made docker reject the flag outright (verified:
# `invalid value for "since": failed to parse value as time or duration`),
# silently defeating this whole check. Fail loudly if log retrieval itself
# errors, instead of treating a failed docker logs call as "no error found".
npu_logs=$(docker logs --since "$start_ts" "$DLSPS_CONTAINER" 2>&1) || {
  echo "FAIL: could not retrieve logs from $DLSPS_CONTAINER since $start_ts; cannot confirm absence of NPU errors" >&2
  exit 1
}
if echo "$npu_logs" | grep -qi "NPU.*\(error\|fail\|unsupported\)"; then
  echo "FAIL: NPU-related error found in container logs since instance $iid started; inspect docker logs $DLSPS_CONTAINER" >&2
  exit 1
fi

echo "PASS: DLSPS NPU variant (device=NPU, pre-process-backend=va) instance $iid produced real advancing detections with no NPU compile/plugin errors in logs."

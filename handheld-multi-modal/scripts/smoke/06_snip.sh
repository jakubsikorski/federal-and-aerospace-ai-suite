#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$root/scripts/smoke/_dlsps_lib.sh"

# This test proves the application-owned gvapython crop callback
# (scripts/dlsps/crop_candidates.py) actually produces per-tracked-object
# snip files through the SHIPPED config/dlsps/{cpu,gpu,npu}/config.json +
# compose/handheld-multi-modal.yaml wiring — not a scripts docker-cp'd into a
# bare container. See docs/application/video-contracts.md "Snip mechanism".
#
# Requires: the mounted config.json declares a `cropcandidates`-named
# gvapython element and a `crop-config` parameter mapped to its `kwarg`
# property (see config/dlsps/npu/README.md); compose mounts
# scripts/dlsps -> /opt/handheld-crops (ro), a Settings-schema config JSON
# -> /opt/handheld-crops-config.json (ro), and a writable output dir ->
# /opt/handheld-crops-output. The config path passed here MUST match
# compose's mount, not any host path.
: "${CROPS_HOST_DIR:=$root/data/dlsps/crops}"
: "${CROP_CONFIG_CONTAINER_PATH:=/opt/handheld-crops-config.json}"

dlsps_require_container

if [ ! -d "$CROPS_HOST_DIR" ]; then
  echo "BLOCKED: $CROPS_HOST_DIR does not exist. Create data/dlsps/crop-config.json and data/dlsps/crops/ (writable) and mount them per compose/handheld-multi-modal.yaml before running this test; see scripts/dlsps/README.md." >&2
  exit 2
fi

before_count=$(find "$CROPS_HOST_DIR" -maxdepth 1 -name '*.jpg' 2>/dev/null | wc -l)

echo "Starting NPU pipeline with crop callback enabled..." >&2
DLSPS_PIPELINE_NAME=handheld_npu_detect
crop_params="{\"config\": \"$CROP_CONFIG_CONTAINER_PATH\"}"
resp=$(curl --fail --silent --show-error --max-time 15 \
  -X POST "$DLSPS_REST/pipelines/user_defined_pipelines/$DLSPS_PIPELINE_NAME" \
  -H 'Content-Type: application/json' \
  -d "{\"source\": {\"uri\": \"$DLSPS_DEMO_URI\", \"type\": \"uri\"}, \"destination\": {\"metadata\": {\"type\": \"mqtt\", \"publish_frame\": false, \"topic\": \"detections/live\"}}, \"parameters\": {\"detection-properties\": {\"model\": \"$DLSPS_MODEL\", \"device\": \"NPU\"}, \"crop-config\": $(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$crop_params")}}")
iid=$(python3 -c "import json,sys; print(json.loads(sys.argv[1]))" "$resp")
echo "Started instance $iid" >&2
trap 'dlsps_stop_pipeline "$iid" || echo "WARN: instance $iid may still be running, check manually" >&2' EXIT

dlsps_wait_pipeline "$iid" 20 5 >/dev/null

echo "Letting the crop producer admit tracked objects for 15s..." >&2
sleep 15

after_count=$(find "$CROPS_HOST_DIR" -maxdepth 1 -name '*.jpg' 2>/dev/null | wc -l)
new_crops=$((after_count - before_count))

if [ "$new_crops" -le 0 ]; then
  echo "FAIL: no new .jpg crop files appeared in $CROPS_HOST_DIR (before=$before_count, after=$after_count). Check dlsps container logs for gvapython 'Error creating Python callback' (bad kwarg/config path) or a stale advisory .lock in the output dir requiring a dlsps container restart." >&2
  exit 1
fi

# Each crop must have a matching JSON sidecar (crop_ready event with
# tracker/label/confidence/rect metadata) — a lone .jpg without its sidecar
# indicates the producer wrote the image but failed before its durable
# JSON event, which the real Backend consumer would treat as incomplete.
orphans=0
for f in "$CROPS_HOST_DIR"/*.jpg; do
  [ -e "$f" ] || continue
  base="${f%.jpg}"
  if [ ! -f "${base}.json" ]; then
    orphans=$((orphans + 1))
  fi
done
if [ "$orphans" -gt 0 ]; then
  echo "FAIL: $orphans crop .jpg file(s) in $CROPS_HOST_DIR have no matching .json sidecar." >&2
  exit 1
fi

echo "PASS: crop callback (scripts/dlsps/crop_candidates.py, wired via shipped config.json + Compose) produced $new_crops new matched .jpg+.json snip pairs in $CROPS_HOST_DIR during instance $iid."

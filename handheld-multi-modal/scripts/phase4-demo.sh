#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
ASSET_ROOT=${ASSET_ROOT:-$ROOT}
CAMERAS=${CAMERAS:-2}
WAIT_SECONDS=${PHASE4_WAIT_SECONDS:-360}
export ASSET_ROOT

COMPOSE=(
  docker compose
  -f "$ROOT/compose/bootstrap-compose.yaml"
  -f "$ROOT/compose/cameras.yaml"
  -f "$ROOT/compose/phase4-demo.override.yaml"
)

die() {
  printf 'phase4-demo: %s\n' "$*" >&2
  exit 1
}

wait_for() {
  local description=$1
  shift
  local attempts=$((WAIT_SECONDS / 2))
  for ((i = 0; i < attempts; i++)); do
    if "$@" >/dev/null 2>&1; then
      printf 'ready: %s\n' "$description"
      return 0
    fi
    sleep 2
  done
  die "timed out waiting for $description"
}

paths_ready() {
  local paths=$1
  curl -fsS --max-time 3 http://127.0.0.1:9997/v3/paths/list |
    python3 -c '
import json, sys
wanted = set(sys.argv[1].split(","))
items = json.load(sys.stdin).get("items", [])
ready = {item.get("name") for item in items if item.get("ready")}
raise SystemExit(0 if wanted <= ready else 1)
' "$paths"
}

feeds_ready() {
  local streams=$1
  curl -skfsS --max-time 5 https://127.0.0.1:8443/api/streams |
    python3 -c '
import json, sys
wanted = set(sys.argv[1].split(","))
streams = json.load(sys.stdin)["data"]
healthy = {item["stream_id"] for item in streams if item.get("feed", {}).get("ok") is True}
raise SystemExit(0 if wanted <= healthy else 1)
' "$streams"
}

telemetry_ready() {
  curl -skfsS --max-time 5 https://127.0.0.1:8443/api/metrics |
    python3 -c '
import json, sys
data = json.load(sys.stdin)["data"]
raise SystemExit(0 if data.get("metrics") else 1)
'
}

down() {
  "${COMPOSE[@]}" down --remove-orphans
}

status() {
  "${COMPOSE[@]}" ps
  curl -skfsS --max-time 5 https://127.0.0.1:8443/api/streams |
    python3 -m json.tool || true
}

prepare_assets() {
  command -v docker >/dev/null || die "docker is required"
  command -v curl >/dev/null || die "curl is required"
  command -v python3 >/dev/null || die "python3 is required"
  command -v openssl >/dev/null || die "openssl is required"
  [[ $CAMERAS =~ ^[1-3]$ ]] || die "CAMERAS must be between 1 and 3"
  [[ -e /dev/dri/renderD128 ]] || die "/dev/dri/renderD128 is required"
  [[ -e /dev/accel/accel0 ]] || die "/dev/accel/accel0 is required"

  export RENDER_GID=${RENDER_GID:-$(stat -c '%g' /dev/dri/renderD128)}
  local accel_gid
  accel_gid=$(stat -c '%g' /dev/accel/accel0)
  [[ $accel_gid == "$RENDER_GID" ]] ||
    die "renderD128 GID $RENDER_GID and accel0 GID $accel_gid differ"

  local model="$ASSET_ROOT/data/dlsps/yolo_export/yolo11s_int8_openvino_model"
  local video="$ASSET_ROOT/data/dlsps/demo-video"
  [[ -f "$model/yolo11s.xml" && -f "$model/yolo11s.bin" && -f "$model/metadata.yaml" ]] ||
    die "missing YOLO11s INT8 assets under $model; see docs/bootstrap/video-contracts.md"
  [[ -f "$video/classroom.avi" ]] ||
    die "missing pinned classroom.avi under $video; see docs/bootstrap/video-contracts.md"
  printf '%s  %s\n' \
    61e8501fa5a11fa012921d5f7c65f0ff05fa78f2d2bf0a37fc1fe7fdf8630d45 \
    "$model/yolo11s.xml" |
    sha256sum --check --status || die "YOLO11s XML hash mismatch"
  printf '%s  %s\n' \
    8bb1f5d1b46de8c09fe26d0379072cfddf847e4d627696ccd59374b4a623d51d \
    "$model/yolo11s.bin" |
    sha256sum --check --status || die "YOLO11s BIN hash mismatch"
  printf '%s  %s\n' \
    ca6d1b53d54a40ffda9d4ca1d0ab0cc43f744583324aaf97146c820e2b62a2fa \
    "$video/classroom.avi" |
    sha256sum --check --status || die "classroom.avi hash mismatch"

  if [[ ! -f "$video/classroom.ts" ]]; then
    docker run --rm -v "$video:/video" jrottenberg/ffmpeg:6.1-ubuntu2404 \
      -hide_banner -loglevel warning -fflags +genpts -i /video/classroom.avi \
      -map 0:v:0 -c copy /video/classroom.ts
  fi

  mkdir -p "$ASSET_ROOT/data/backend" "$ASSET_ROOT/data/dlsps/crops"
  local tls="$ASSET_ROOT/config/tls"
  if [[ ! -f "$tls/server.crt" || ! -f "$tls/server.key" ]]; then
    local host_ip
    host_ip=${COTURN_EXTERNAL_IP:-$(ip -4 -o addr show scope global |
      awk 'NR==1{split($4,a,"/"); print a[1]}')}
    [[ -n $host_ip ]] || die "no host IPv4 address available for TLS"
    mkdir -p "$tls"
    openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
      -keyout "$tls/server.key" -out "$tls/server.crt" \
      -subj "/CN=$(hostname)" \
      -addext "subjectAltName=DNS:$(hostname),DNS:localhost,IP:$host_ip,IP:127.0.0.1"
    chmod 600 "$tls/server.key"
  fi
  export COTURN_EXTERNAL_IP=${COTURN_EXTERNAL_IP:-$(ip -4 -o addr show scope global |
    awk 'NR==1{split($4,a,"/"); print a[1]}')}
  [[ -n $COTURN_EXTERNAL_IP ]] || die "no host IPv4 address available for Coturn"
}

up() {
  prepare_assets
  "${COMPOSE[@]}" config --quiet

  trap 'code=$?; trap - ERR; printf "phase4-demo: startup failed; tearing down\n" >&2; down || true; exit "$code"' ERR
  "${COMPOSE[@]}" up -d coturn mediamtx mqtt metrics dlsps
  wait_for "MediaMTX" curl -fsS --max-time 3 http://127.0.0.1:9997/v3/paths/list
  wait_for "DLSPS" curl -fsS --max-time 3 http://127.0.0.1:8080/pipelines
  wait_for "Metrics Manager" curl -fsS --max-time 3 http://127.0.0.1:9090/health

  local cameras=()
  local paths=()
  for ((i = 1; i <= CAMERAS; i++)); do
    cameras+=("cam$i")
    paths+=("cam$i")
  done
  "${COMPOSE[@]}" up -d "${cameras[@]}"
  wait_for "clean RTSP publishers" paths_ready "$(IFS=,; echo "${paths[*]}")"

  "${COMPOSE[@]}" up -d --build --no-deps backend
  "${COMPOSE[@]}" up -d --no-deps proxy
  wait_for "Backend through TLS proxy" \
    curl -skfsS --max-time 5 https://127.0.0.1:8443/api/streams
  wait_for "Backend MQTT feed state" feeds_ready "$(IFS=,; echo "${paths[*]}")"

  local annotated=()
  for ((i = 1; i <= CAMERAS; i++)); do
    annotated+=("annotated$i")
  done
  wait_for "annotated WebRTC publishers" paths_ready "$(IFS=,; echo "${annotated[*]}")"
  wait_for "Backend telemetry" telemetry_ready
  local dashboard
  dashboard=$(curl -skfsS --max-time 5 https://127.0.0.1:8443/)
  [[ $dashboard == *"Pause list updates"* ]]
  trap - ERR
  printf 'Phase 4 demo ready: https://127.0.0.1:8443/\n'
  printf 'Feeds: %s | device: NPU | MQTT + WebRTC enabled\n' \
    "$(IFS=,; echo "${paths[*]}")"
}

case ${1:-} in
  up) up ;;
  down) down ;;
  status) status ;;
  *) die "usage: $0 {up|down|status}" ;;
esac

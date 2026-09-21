# Repeatable operator entry points. See docs/bootstrap/application-operations.md.
SHELL := /bin/bash
.SHELLFLAGS := -Eeuo pipefail -c
.ONESHELL:
.DEFAULT_GOAL := help

CAMERAS ?= 2
ASSET_ROOT ?= $(CURDIR)
export ASSET_ROOT := $(abspath $(ASSET_ROOT))
export WAIT_SECONDS ?= 360
HOST_NAME ?= $(shell hostname)
HOST_IP ?= $(shell ip -4 -o addr show scope global | awk 'NR==1{split($$4,a,"/"); print a[1]}')
export COTURN_EXTERNAL_IP ?= $(HOST_IP)
export RENDER_GID ?= $(shell stat -c '%g' /dev/dri/renderD128 2>/dev/null)
export CAMERAS
TLS_DIR := $(ASSET_ROOT)/config/tls
DEMO_COMPOSE := docker compose -f compose/bootstrap-compose.yaml -f compose/cameras.yaml -f compose/phase4-demo.override.yaml

.PHONY: help tls demo-setup demo-up demo-stop demo-restart demo-down demo-status cameras cameras-down health test test-ui
help:
	@echo 'make demo-up       Verify assets, prepare device, start and check application'
	@echo 'make demo-stop     Stop Backend and DLSPS (including stuck pipelines)'
	@echo 'make demo-restart  Stop Backend/DLSPS, then start and verify application'
	@echo 'make demo-down     Remove application containers; preserve assets and history'
	@echo 'make demo-status  Show containers and camera feeds'
	@echo 'Options: CAMERAS=1..3 ASSET_ROOT=/path/to/assets HOST_IP=<LAN IP>'

# Only this small polling helper is shared between startup checks.
define readiness
wait_for() {
  local description=$$1
  shift
  local attempts=$$((WAIT_SECONDS / 2))
  for ((i = 0; i < attempts; i++)); do
    if "$$@" >/dev/null 2>&1; then
      printf 'ready: %s\n' "$$description"
      return 0
    fi
    sleep 2
  done
  echo "Timed out waiting for $$description" >&2; return 1
}

paths_ready() {
  local paths=$$1
  curl -fsS --max-time 3 http://127.0.0.1:9997/v3/paths/list |
    python3 -c '
import json, sys
wanted = set(sys.argv[1].split(","))
items = json.load(sys.stdin).get("items", [])
ready = {item.get("name") for item in items if item.get("ready")}
raise SystemExit(0 if wanted <= ready else 1)
' "$$paths"
}

feeds_ready() {
  local streams=$$1
  curl -skfsS --max-time 5 https://127.0.0.1:8443/api/streams |
    python3 -c '
import json, sys
wanted = set(sys.argv[1].split(","))
streams = json.load(sys.stdin)["data"]
healthy = {item["stream_id"] for item in streams if item.get("feed", {}).get("ok") is True}
raise SystemExit(0 if wanted <= healthy else 1)
' "$$streams"
}

telemetry_ready() {
  curl -skfsS --max-time 5 https://127.0.0.1:8443/api/metrics |
    python3 -c '
import json, sys
data = json.load(sys.stdin)["data"]
raise SystemExit(0 if data.get("metrics") else 1)
'
}

endef

tls:
	@test -n "$(COTURN_EXTERNAL_IP)" || { echo 'Set HOST_IP to the device LAN address' >&2; exit 1; }
	if [[ ! -f "$(TLS_DIR)/server.crt" || ! -f "$(TLS_DIR)/server.key" ]]; then
	  mkdir -p "$(TLS_DIR)"
	  openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
	    -keyout "$(TLS_DIR)/server.key" -out "$(TLS_DIR)/server.crt" \
	    -subj "/CN=$(HOST_NAME)" \
	    -addext "subjectAltName=DNS:$(HOST_NAME),DNS:localhost,IP:$(COTURN_EXTERNAL_IP),IP:127.0.0.1"
	  chmod 600 "$(TLS_DIR)/server.key"
	fi

demo-setup: tls
	@fail() { echo "demo-setup: $$*" >&2; exit 1; }
	command -v docker >/dev/null || fail "docker is required"
	command -v curl >/dev/null || fail "curl is required"
	command -v python3 >/dev/null || fail "python3 is required"
	command -v openssl >/dev/null || fail "openssl is required"
	[[ $$CAMERAS =~ ^[1-3]$$ ]] || fail "CAMERAS must be between 1 and 3"
	[[ -e /dev/dri/renderD128 ]] || fail "/dev/dri/renderD128 is required"
	[[ -e /dev/accel/accel0 ]] || fail "/dev/accel/accel0 is required"

	export RENDER_GID=$${RENDER_GID:-$$(stat -c '%g' /dev/dri/renderD128)}
	accel_gid=$$(stat -c '%g' /dev/accel/accel0)
	[[ $$accel_gid == "$$RENDER_GID" ]] ||
	  fail "renderD128 GID $$RENDER_GID and accel0 GID $$accel_gid differ"

	model="$$ASSET_ROOT/data/dlsps/yolo_export/yolo11s_int8_openvino_model"
	video="$$ASSET_ROOT/data/dlsps/demo-video"
	[[ -f "$$model/yolo11s.xml" && -f "$$model/yolo11s.bin" && -f "$$model/metadata.yaml" ]] ||
	  fail "missing YOLO11s INT8 assets under $$model; see docs/bootstrap/video-contracts.md"
	[[ -f "$$video/classroom.avi" ]] ||
	  fail "missing pinned classroom.avi under $$video; see docs/bootstrap/video-contracts.md"
	printf '%s  %s\n' \
	  61e8501fa5a11fa012921d5f7c65f0ff05fa78f2d2bf0a37fc1fe7fdf8630d45 \
	  "$$model/yolo11s.xml" |
	  sha256sum --check --status || fail "YOLO11s XML hash mismatch"
	printf '%s  %s\n' \
	  8bb1f5d1b46de8c09fe26d0379072cfddf847e4d627696ccd59374b4a623d51d \
	  "$$model/yolo11s.bin" |
	  sha256sum --check --status || fail "YOLO11s BIN hash mismatch"
	printf '%s  %s\n' \
	  ca6d1b53d54a40ffda9d4ca1d0ab0cc43f744583324aaf97146c820e2b62a2fa \
	  "$$video/classroom.avi" |
	  sha256sum --check --status || fail "classroom.avi hash mismatch"

	if [[ ! -f "$$video/classroom.ts" ]]; then
	  docker run --rm -v "$$video:/video" jrottenberg/ffmpeg:6.1-ubuntu2404 \
	    -hide_banner -loglevel warning -fflags +genpts -i /video/classroom.avi \
	    -map 0:v:0 -c copy /video/classroom.ts
	fi

	mkdir -p "$$ASSET_ROOT/data/backend" "$$ASSET_ROOT/data/dlsps/crops"
	sed -e 's|webrtcAdditionalHosts:.*|webrtcAdditionalHosts: [127.0.0.1, $(COTURN_EXTERNAL_IP)]|' \
	    -e 's|:127.0.0.1:3478|:$(COTURN_EXTERNAL_IP):3478|g' \
	    config/mediamtx/mediamtx.yml > "$$ASSET_ROOT/config/mediamtx.yml"

demo-up: demo-setup
	@$(readiness)
	$(DEMO_COMPOSE) config --quiet

	trap 'code=$$?; trap - ERR; printf "demo-up: startup failed; stopping pipelines\n" >&2; $(DEMO_COMPOSE) stop backend dlsps || true; exit "$$code"' ERR
	$(DEMO_COMPOSE) stop backend dlsps
	$(DEMO_COMPOSE) stop cam1 cam2 cam3 cam4
	$(DEMO_COMPOSE) up -d coturn mediamtx mqtt metrics dlsps
	wait_for "MediaMTX" curl -fsS --max-time 3 http://127.0.0.1:9997/v3/paths/list
	wait_for "DLSPS" curl -fsS --max-time 3 http://127.0.0.1:8080/pipelines
	wait_for "Metrics Manager" curl -fsS --max-time 3 http://127.0.0.1:9090/health

	cameras=()
	paths=()
	for ((i = 1; i <= CAMERAS; i++)); do
	  cameras+=("cam$$i")
	  paths+=("cam$$i")
	done
	$(DEMO_COMPOSE) up -d --force-recreate "$${cameras[@]}"
	wait_for "clean RTSP publishers" paths_ready "$$(IFS=,; echo "$${paths[*]}")"

	$(DEMO_COMPOSE) up -d --build --no-deps backend
	$(DEMO_COMPOSE) up -d --no-deps proxy
	wait_for "Backend through TLS proxy" \
	  curl -skfsS --max-time 5 https://127.0.0.1:8443/api/streams
	wait_for "Backend MQTT feed state" feeds_ready "$$(IFS=,; echo "$${paths[*]}")"

	annotated=()
	for ((i = 1; i <= CAMERAS; i++)); do
	  annotated+=("annotated$$i")
	done
	wait_for "annotated WebRTC publishers" paths_ready "$$(IFS=,; echo "$${annotated[*]}")"
	wait_for "Backend telemetry" telemetry_ready
	dashboard=$$(curl -skfsS --max-time 5 https://127.0.0.1:8443/)
	[[ $$dashboard == *'id="pause-updates"'* ]]
	trap - ERR
	printf 'Phase 4 demo ready: https://127.0.0.1:8443/\n'
	printf 'Feeds: %s | device: NPU | MQTT + WebRTC enabled\n' \
	  "$$(IFS=,; echo "$${paths[*]}")"

demo-stop:
	$(DEMO_COMPOSE) stop backend dlsps

# Every up starts with a pipeline drain; repeat deployment uses the same path.
demo-restart: demo-up

demo-down:
	$(DEMO_COMPOSE) down --remove-orphans

demo-status:
	$(DEMO_COMPOSE) ps
	curl -skf --max-time 5 https://127.0.0.1:8443/api/streams | python3 -m json.tool

cameras: demo-setup
	$(DEMO_COMPOSE) up -d --no-deps $(foreach i,$(shell seq 1 $(CAMERAS)),cam$(i))

cameras-down:
	$(DEMO_COMPOSE) stop cam1 cam2 cam3 cam4

health:
	curl -skf --max-time 5 https://127.0.0.1:8443/api/health | python3 -m json.tool

test:
	$(DEMO_COMPOSE) exec -T backend python3 test_backend.py

test-ui:
	python3 backend/test_ui.py

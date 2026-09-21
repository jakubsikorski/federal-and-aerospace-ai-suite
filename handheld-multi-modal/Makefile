# Repeatable operator entry points. See docs/bootstrap/application-operations.md.
SHELL := /bin/bash
.SHELLFLAGS := -Eeuo pipefail -c
.ONESHELL:
.DEFAULT_GOAL := help

CAMERAS ?= 2
ASSET_ROOT ?= $(CURDIR)
export ASSET_ROOT := $(abspath $(ASSET_ROOT))
export WAIT_SECONDS ?= 360
MODEL_WAIT_SECONDS ?= 3600
AI_WAIT_SECONDS ?= 3600
HOST_NAME ?= $(shell hostname)
HOST_IP ?= $(shell ip -4 -o addr show scope global | awk 'NR==1{split($$4,a,"/"); print a[1]}')
export COTURN_EXTERNAL_IP ?= $(HOST_IP)
export RENDER_GID ?= $(shell stat -c '%g' /dev/dri/renderD128 2>/dev/null)
export ACCEL_GID ?= $(shell stat -c '%g' /dev/accel/accel0 2>/dev/null)
export ASSET_GID := $(shell id -g)
export CAMERAS
TLS_DIR := $(ASSET_ROOT)/config/tls
ASSET_COMPOSE := docker compose -f compose/bootstrap-compose.yaml
DEMO_COMPOSE := $(ASSET_COMPOSE) -f compose/cameras.yaml -f compose/phase4-demo.override.yaml -f "$(ASSET_ROOT)/config/devices.yaml"

.PHONY: help tls model-downloader-up demo demo-assets demo-preflight demo-setup demo-up demo-stop demo-restart demo-down demo-status cameras cameras-down health test test-ui test-assets test-phase5
help:
	@echo 'make demo          Download assets, prepare device, start and check the entire stack'
	@echo 'make demo-assets   Download YOLO11s INT8 through Model Download and the pinned video'
	@echo 'make demo-up       Same complete setup/start flow as demo'
	@echo 'make model-downloader-up  Start stock downloader (--plugins ultralytics, port 8200)'
	@echo 'make demo-stop     Stop all application services (including stuck pipelines)'
	@echo 'make demo-restart  Restart all services and verify application'
	@echo 'make demo-down     Remove application containers; preserve assets and history'
	@echo 'make demo-status  Show containers and camera feeds'
	@echo 'make test-phase5  Verify typed history against the running full demo'
	@echo 'Options: CAMERAS=1..3 ASSET_ROOT=/path/to/assets HOST_IP=<LAN IP>'

# Only this small polling helper is shared between startup checks.
define readiness
wait_for() {
  local description=$$1
  shift
  local deadline=$$((SECONDS + WAIT_SECONDS))
  while ((SECONDS < deadline)); do
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

json_ready() {
  curl -fsS --max-time 5 "$$1" |
    python3 -c 'import json,sys; sys.exit(json.load(sys.stdin).get(sys.argv[1]) != sys.argv[2])' "$$2" "$$3"
}

endef

model-downloader-up:
	@$(readiness)
	install -d -m 2775 "$$ASSET_ROOT/data/model-download"
	$(ASSET_COMPOSE) up -d --no-deps model-downloader
	WAIT_SECONDS=$(MODEL_WAIT_SECONDS) wait_for "Model Download (ultralytics, port 8200)" curl -fsS --max-time 5 http://127.0.0.1:8200/api/v1/health

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

# These prerequisites are deliberately sequential, including under make -j.
.NOTPARALLEL: demo-assets demo-setup demo-restart

demo-assets:
	@if python3 scripts/demo_assets.py check-model; then
	  echo 'YOLO11s INT8 cache verified'
	else
	  code=$$?
	  [[ $$code == 2 ]] || exit "$$code"
	  make --no-print-directory model-downloader-up
	  python3 scripts/demo_assets.py model --timeout $(MODEL_WAIT_SECONDS)
	fi
	python3 scripts/demo_assets.py video
	video="$$ASSET_ROOT/data/dlsps/demo-video"

	if [[ ! -f "$$video/classroom.ts" ]]; then
	  docker run --rm --user "$$(id -u):$$(id -g)" -v "$$video:/video" jrottenberg/ffmpeg:6.1-ubuntu2404 \
	    -hide_banner -loglevel warning -y -fflags +genpts -i /video/classroom.avi \
	    -map 0:v:0 -c copy -f mpegts /video/classroom.part.ts
	  mv "$$video/classroom.part.ts" "$$video/classroom.ts"
	fi

demo-preflight:
	@fail() { echo "demo-preflight: $$*" >&2; exit 1; }
	command -v docker >/dev/null || fail "docker is required"
	command -v curl >/dev/null || fail "curl is required"
	command -v python3 >/dev/null || fail "python3 is required"
	command -v openssl >/dev/null || fail "openssl is required"
	[[ $$CAMERAS =~ ^[1-3]$$ ]] || fail "CAMERAS must be between 1 and 3"
	[[ -e /dev/dri/renderD128 ]] || fail "/dev/dri/renderD128 is required"
	[[ -e /dev/accel/accel0 ]] || fail "/dev/accel/accel0 is required"

demo-setup: demo-preflight demo-assets tls
	@mkdir -p "$$ASSET_ROOT/data/backend"
	install -d -m 2775 "$$ASSET_ROOT/data/dlsps/crops" "$$ASSET_ROOT/data/chatqna/model_cache"
	python3 -c 'import json,os; print("services:"); [print("  " + service + ":\n    group_add: !override " + json.dumps(list(dict.fromkeys(os.environ[k] for k in keys)))) for service,keys in {"dlsps":("RENDER_GID","ACCEL_GID","ASSET_GID"), "chatqna":("RENDER_GID","ASSET_GID")}.items()]' > "$$ASSET_ROOT/config/devices.yaml"

	sed -e 's|webrtcAdditionalHosts:.*|webrtcAdditionalHosts: [127.0.0.1, $(COTURN_EXTERNAL_IP)]|' \
	    -e 's|:127.0.0.1:3478|:$(COTURN_EXTERNAL_IP):3478|g' \
	    config/mediamtx/mediamtx.yml > "$$ASSET_ROOT/config/mediamtx.yml"

demo: demo-up

demo-up: demo-setup
	@$(readiness)
	$(DEMO_COMPOSE) config --quiet

	trap 'code=$$?; trap - ERR; printf "demo-up: startup failed; stopping full demo stack\n" >&2; $(DEMO_COMPOSE) --profile assets stop || true; exit "$$code"' ERR
	$(DEMO_COMPOSE) stop backend dlsps chatqna
	# Stock conversion skips any existing directory, even an interrupted export.
	# Preserve partial output before retrying with the unmodified stock backend.
	$(DEMO_COMPOSE) run --rm --no-deps --entrypoint python3 \
	  -v "$(CURDIR)/scripts:/bootstrap/scripts:ro" \
	  -v "$(CURDIR)/config/model-download:/bootstrap/config/model-download:ro" \
	  -e PYTHONPATH=/bootstrap/scripts chatqna -c 'import pathlib,yaml; from demo_assets import repair_chat_cache; models=yaml.safe_load(pathlib.Path("/tmp/model_config/config.yaml").read_text())["model_settings"]; repair_chat_cache(pathlib.Path("/tmp/model_cache"), [models[role + "_MODEL_ID"] for role in ("LLM", "EMBEDDING", "RERANKER")])'

	$(DEMO_COMPOSE) stop cam1 cam2 cam3 cam4
	$(DEMO_COMPOSE) up -d coturn mediamtx mqtt metrics dlsps alert-agent chatqna audio-analyzer tts
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
	WAIT_SECONDS=$(AI_WAIT_SECONDS) wait_for "Chat Q&A neural-chat model" json_ready http://127.0.0.1:8888/model llm_model Intel/neural-chat-7b-v3-3
	WAIT_SECONDS=$(AI_WAIT_SECONDS) wait_for "Whisper GPU service" json_ready http://127.0.0.1:8011/health status ok
	WAIT_SECONDS=$(AI_WAIT_SECONDS) wait_for "SpeechT5 GPU service" json_ready http://127.0.0.1:8012/v1/model-info device GPU

	wait_for "Alert Agent" curl -fsS --max-time 5 http://127.0.0.1:8010/api/v1/health

	dashboard=$$(curl -skfsS --max-time 5 https://127.0.0.1:8443/)
	[[ $$dashboard == *'id="pause-updates"'* ]]
	trap - ERR
	printf 'Full demo stack ready: https://127.0.0.1:8443/\n'
	printf 'Feeds: %s | device: NPU | MQTT + WebRTC enabled\n' \
	  "$$(IFS=,; echo "$${paths[*]}")"

demo-stop:
	$(DEMO_COMPOSE) --profile assets stop

# Restart every application service; reuse verified models and retained caches.
demo-restart: demo-stop demo-up

demo-down:
	$(DEMO_COMPOSE) --profile assets down --remove-orphans

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

test-assets:
	python3 scripts/test_demo_assets.py

test-ui:
	python3 backend/test_ui.py

test-phase5:
	python3 scripts/smoke/14_phase5_history.py

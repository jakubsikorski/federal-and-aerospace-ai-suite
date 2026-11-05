# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Deterministic Threat Detection: TSN demonstration for Intel NIC.
#
#   make up [SOURCE=video|basler] [STREAMS=0|1]
#                  start everything: the RTSP streams on the camera machines, the dashboard (with the MQTT
#                  broker), the load agent on machine-4, the camera pipelines on the camera machines and the
#                  scene stack (SceneScape, DL Streamer, mediamtx)
#   make down      stop everything (all machines at the same time)
#   make status    what is running
#   make models    download the detection model (done by `make up`; asks to accept the license first)
#   make help      this text
#
# SOURCE: where use case 2 gets its video. Give it on the command line, not in a file.
#   video   (default) two test videos, served as RTSP "cameras" by machine-2 and machine-3. HOTA is shown.
#   basler  two Basler GigE cameras on switch ports 5 and 6 (GenICam, PTP timestamps). The tracker rate
#           is shown; HOTA is not (no ground truth). Prepare the cameras first, see
#           docs/user-guide/how-to-guides/basler/.
#   Pass SOURCE=basler on every `make up` (the default is video); the dashboard diagram follows it. Use case 1 is the same
#   for both and always needs the test-video streams on machine-2/3.
# STREAMS: 1 (default) starts the test-video streams. Do not set 0, not even with SOURCE=basler:
#   use case 1 reads them on machine-2/3. Use 0 only if you start the streams yourself.
#
SHELL := /bin/bash
STREAMS ?= 1
# UC2 camera source (video|basler), from the command line only: `make up SOURCE=basler`; default video. The dashboard diagram follows it.
SOURCE ?= video
export SOURCE
-include lab.env
# This machine is machine-1, so its lab address is M1_LAB_IP (falls back to the default-route address)
LAB_IP ?= $(or $(M1_LAB_IP),$(shell ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($$i=="src") print $$(i+1)}'))
STREAMS_SCRIPT := src/camera-node/streams/start-synced-streams.sh

.PHONY: help up down status models test test-e2e streams-up streams-down streams-status

help:
	@awk 'NR>=4 { if ($$0 !~ /^#/) exit; sub(/^# ?/, ""); print }' Makefile

up: models
	@[ "$(STREAMS)" != 1 ] || $(MAKE) streams-up
	# the dashboard mounts the SceneScape broker's CA certificate, which the secrets step creates
	$(MAKE) -C src/scene secrets
	$(MAKE) -C src/dashboard up
	src/traffic-node/deploy.sh up
	STREAMS=0 src/camera-node/pipeline/deploy.sh up
	$(MAKE) -C src/scene up STREAMS=0
	@echo ""
	@echo "Everything is up."
	@echo "  Dashboard:  https://$(LAB_IP)/dashboard/   (self-signed certificate: accept the browser warning)"
	@echo "  SceneScape: https://$(LAB_IP)/"

# every part is stopped at the same time; failures in one do not stop the others
down:
	@$(MAKE) -C src/scene down STREAMS=0 & \
	STREAMS=0 src/camera-node/pipeline/deploy.sh down & \
	$(MAKE) -C src/dashboard down & \
	src/traffic-node/deploy.sh down & \
	{ [ "$(STREAMS)" != 1 ] || $(MAKE) streams-down; } & \
	wait

status:
	@echo "== dashboard"; $(MAKE) -s -C src/dashboard status
	@echo "== traffic node: metrics-manager + load agent (machine-4)"; src/traffic-node/deploy.sh status
	@echo "== camera nodes (machine-2/3)"; src/camera-node/pipeline/deploy.sh status
	@echo "== scene stack"; $(MAKE) -s -C src/scene status

# downloads the detection model on first use (needs internet access on machine-1)
models:
	src/data/download_models.sh

test:
	$(MAKE) -C src/dashboard test

test-e2e:
	$(MAKE) -C src/scene test

streams-up:
	$(STREAMS_SCRIPT) up

streams-down:
	$(STREAMS_SCRIPT) down

streams-status:
	$(STREAMS_SCRIPT) status

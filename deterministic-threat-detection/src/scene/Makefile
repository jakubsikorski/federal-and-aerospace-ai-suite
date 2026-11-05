# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Standalone deterministic threat detection (HOTA) application.
#   make up [SOURCE=video|basler] [HOST_IP=x.x.x.x]   generate secrets/certs, configure .env, start the stack
#   make down                   stop the stack (keeps volumes) and the remote streams
#   make clean                  stop the stack and delete volumes (and stop the streams)
#   make streams-up|streams-down|streams-status   RTSP cameras on the remote machines only
#   STREAMS=0 skips the remote streams in up/down/clean
#   make status | logs [SVC=name]
#   make test                   end-to-end test through the SceneScape UI and data flow
#   make distclean              clean + remove generated secrets and certs

SHELL := /bin/bash
# Defaults to the TSN VLAN1 address on tsn0.1 (192.168.127.11); falls back to the first host IP.
TSN_IFACE ?= tsn0.1
HOST_IP ?= $(or $(shell ip -4 -o addr show dev $(TSN_IFACE) 2>/dev/null | awk '{split($$4,a,"/"); print a[1]; exit}'),$(shell hostname -I | cut -f1 -d' '))
SVC ?=
# Address of this machine on the lab network (default route), advertised for WebRTC
LAB_IP ?= $(shell ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($$i=="src") print $$(i+1)}')
# Remote RTSP cameras (host:camera pairs), started in lockstep over ssh by ../camera-node/streams/start-synced-streams.sh
STREAMS ?= 1
STREAM_HOSTS ?= tsn-machine-2:Cam_x1_0 tsn-machine-3:Cam_x2_0
export STREAM_HOSTS
# Hosts the web UI accepts (Django ALLOWED_HOSTS). '*' = any host/IP; restrict with make up ALLOWED_HOSTS=a,b
ALLOWED_HOSTS ?= *

# Camera source: video (default) or basler, from the command line only.
SOURCE ?= video
export SOURCE
# The Basler source adds the pylon image and a macvlan on the TSN VLAN
ifeq ($(SOURCE),basler)
export COMPOSE_FILE := docker-compose.yml:docker-compose.basler.yml
endif

SECRETS_DIR := secrets
SSL_DIR := nginx/ssl

.PHONY: up down clean distclean status logs env secrets nginx-cert pull test streams-up streams-down streams-status

# Streams first: consumers get 404 until the publishers exist.
up: env nginx-cert
	@[ "$(STREAMS)" != 1 ] || $(MAKE) streams-up
	@if [ "$(SOURCE)" = basler ]; then v=$$(sed -n 's/^SCENESCAPE_VERSION=//p' .env); \
	  docker image inspect "tsn-dlstreamer-pipeline-server-basler:$$v" >/dev/null 2>&1 || { \
	  echo "Image tsn-dlstreamer-pipeline-server-basler:$$v not found. Build it first (it contains the Basler pylon SDK, under Basler's own license): docs/user-guide/how-to-guides/basler/integrate-basler-camera-with-scenescape.md"; exit 1; }; fi
	docker compose up -d
	@echo "Browse to https://localhost/ (admin / contents of $(SECRETS_DIR)/supass)."
	@[ "$(STREAMS)" = 1 ] || echo "STREAMS=0: the RTSP streams on CAMERA1_HOST/CAMERA2_HOST (see .env) must already be running."

streams-up:
	../camera-node/streams/start-synced-streams.sh up

streams-down:
	../camera-node/streams/start-synced-streams.sh down

streams-status:
	../camera-node/streams/start-synced-streams.sh status

secrets: $(SECRETS_DIR)/browser.auth

$(SECRETS_DIR)/browser.auth:
	bash $(SECRETS_DIR)/generate_secrets.sh

# SUPASS is kept in sync with the generated secret; UID/GID stay blank because
# the broker entrypoint copies secrets under a root-owned directory.
env: secrets
	@[ -f .env ] || cp .env.example .env
	@# keys added to .env.example after .env was created
	@grep -E '^[A-Z0-9_]+=' .env.example | while IFS= read -r l; do grep -q "^$${l%%=*}=" .env || echo "$$l" >> .env; done
	sed -i "s#^SUPASS=.*#SUPASS=$$(cat $(SECRETS_DIR)/supass)#" .env
	sed -i "s#^HOST_IP=.*#HOST_IP=$(HOST_IP)#" .env
	grep -q "^LAB_IP=" .env || echo "LAB_IP=" >> .env
	sed -i "s#^LAB_IP=.*#LAB_IP=$(LAB_IP)#" .env
	grep -q "^SOURCE=" .env || echo "SOURCE=" >> .env
	sed -i "s#^SOURCE=.*#SOURCE=$(SOURCE)#" .env
	grep -q "^COMPOSE_PROFILES=" .env || echo "COMPOSE_PROFILES=" >> .env
	sed -i "s#^COMPOSE_PROFILES=.*#COMPOSE_PROFILES=$(SOURCE)#" .env
	python3 dlstreamer-pipeline-server/render_config.py dlstreamer-pipeline-server/config.generated.json .env
	sed -i "s#^UID=.*#UID=#; s#^GID=.*#GID=#" .env
	sed -i "s#^ALLOWED_HOSTS=.*#ALLOWED_HOSTS=$(ALLOWED_HOSTS)#" .env

nginx-cert: $(SSL_DIR)/server.key

$(SSL_DIR)/server.key:
	mkdir -p $(SSL_DIR)
	openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
		-keyout $(SSL_DIR)/server.key -out $(SSL_DIR)/server.crt \
		-subj "/C=US/ST=CA/L=San Francisco/O=Intel/OU=Edge AI/CN=localhost"

pull:
	docker compose pull

down:
	COMPOSE_FILE=docker-compose.yml:docker-compose.basler.yml docker compose down
	@[ "$(STREAMS)" != 1 ] || $(MAKE) streams-down

clean:
	docker compose down -v
	@[ "$(STREAMS)" != 1 ] || $(MAKE) streams-down

distclean: clean
	find $(SECRETS_DIR) -mindepth 1 ! -name generate_secrets.sh ! -name openssl.cnf -delete
	rm -rf $(SSL_DIR)

status:
	docker compose ps

logs:
	docker compose logs --tail=100 -f $(SVC)

# Needs the stack up and the RTSP streams running. Uses a throwaway venv.
test:
	python3 -m venv .venv-test
	.venv-test/bin/pip install -q playwright requests
	.venv-test/bin/playwright install chromium
	.venv-test/bin/python tests/e2e_test.py

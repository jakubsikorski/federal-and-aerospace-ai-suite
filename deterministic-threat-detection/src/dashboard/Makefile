# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# The shared dashboard: web UI + backend + the MQTT broker for the camera results.
#   make up | down | status | logs     build and run broker + backend (docker compose)
#   make deploy-switch                 copy the Moxa scripts to the control host (machine-2)
#   make fake                          feed the dashboard with fake inference messages
#   make test                          unit tests (throwaway venv)
#   make dev-backend                   run the backend from source (simulated controls)
# ../../Makefile starts everything.

SHELL := /bin/bash
SWITCH_HOST ?= tsndemo@10.106.147.105
LAB_IP ?= $(shell ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($$i=="src") print $$(i+1)}')

.PHONY: env up down status logs deploy-switch fake test dev-backend

env:
	@[ -f .env ] || cp .env.example .env
	sed -i "s#^LOCAL_UID=.*#LOCAL_UID=$$(id -u)#; s#^LOCAL_GID=.*#LOCAL_GID=$$(id -g)#" .env
	@grep -q '^DASHBOARD_BIND=.\+' .env || sed -i "s#^DASHBOARD_BIND=.*#DASHBOARD_BIND=$(LAB_IP)#" .env

up: env
	docker compose up -d --build
	@echo "Dashboard: http://$$(grep ^DASHBOARD_BIND= .env | cut -d= -f2):$$(grep ^DASHBOARD_PORT= .env | cut -d= -f2)/"

down:
	docker compose down

status:
	docker compose ps

logs:
	docker compose logs --tail=100 -f

deploy-switch:
	ssh -o BatchMode=yes $(SWITCH_HOST) 'mkdir -p ~/moxa'
	scp -o BatchMode=yes ../switch/get_port_mode.py ../switch/tas_config.py ../switch/port_priority.py $(SWITCH_HOST):moxa/

fake:
	python3 -I tools/fake_publisher.py --port $$(grep ^MQTT_PORT= .env | cut -d= -f2) --api http://$$(grep ^DASHBOARD_BIND= .env | cut -d= -f2):8080

test:
	python3 -m venv .venv-test
	.venv-test/bin/pip install -q -r backend/requirements.txt pytest httpx
	cd backend && ../.venv-test/bin/python -m pytest -q -p no:cacheprovider

dev-backend:
	cd backend && MQTT_PORT=1884 python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8090

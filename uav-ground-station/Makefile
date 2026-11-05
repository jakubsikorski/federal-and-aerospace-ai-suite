# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

.PHONY: help init build _build_if_changed up up-vss _check_vss_env down down-vss clean-vss-index delete-vss-volumes logs

ifneq (,$(wildcard .env))
  include .env
  export
endif

help: ## Show available targets
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / \
	  {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

init: ## Create .env from template if missing, and auto-detect host IP and GPU/NPU device paths
	@if test -f .env; then \
		echo ".env already exists — skipping (delete it to re-run init)"; \
	else \
		cp .env.example .env; \
		echo "[init] .env ready. Edit VIZ_COMPANION_HOST to point at your UAV companion-compute board's IP."; \
		HOST_IP_DETECTED=$$(hostname -I 2>/dev/null | awk '{print $$1}'); \
		if [ -z "$$HOST_IP_DETECTED" ]; then \
			HOST_IP_DETECTED=127.0.0.1; \
			echo "⚠  Could not auto-detect host IP — defaulting VIZ_HOST_IP/VIZ_PUBLIC_HOST=127.0.0.1 (edit .env manually if needed)."; \
		else \
			echo "✅ Host IP detected:"; \
			echo "   VIZ_HOST_IP=$$HOST_IP_DETECTED"; \
			echo "   VIZ_PUBLIC_HOST=$$HOST_IP_DETECTED"; \
			echo "   ⚠  Security: the dashboard (8080), mediamtx WebRTC (8889/8189) and VSS"; \
			echo "      Pipeline Manager (3001) ports will be published on this LAN address."; \
			echo "      Set VIZ_HOST_IP=127.0.0.1 and VIZ_PUBLIC_HOST=127.0.0.1 in .env for"; \
			echo "      loopback-only access if this host is not on a trusted, isolated network."; \
		fi; \
		sed -i "s|^VIZ_HOST_IP=.*|VIZ_HOST_IP=$$HOST_IP_DETECTED|" .env; \
		sed -i "s|^VIZ_PUBLIC_HOST=.*|VIZ_PUBLIC_HOST=$$HOST_IP_DETECTED|" .env; \
		GPU_CARD=$$(ls -1 /dev/dri/card* 2>/dev/null | head -1); \
		GPU_RENDER=$$(ls -1 /dev/dri/renderD* 2>/dev/null | head -1); \
		if [ -n "$$GPU_CARD" ] && [ -n "$$GPU_RENDER" ]; then \
			echo "✅ GPU detected: GPU_DEVICE=$$GPU_CARD GPU_RENDER_DEVICE=$$GPU_RENDER"; \
			sed -i "s|^GPU_DEVICE=.*|GPU_DEVICE=$$GPU_CARD|" .env; \
			sed -i "s|^GPU_RENDER_DEVICE=.*|GPU_RENDER_DEVICE=$$GPU_RENDER|" .env; \
		else \
			echo "⚠  No Intel GPU found at /dev/dri/ — leaving GPU_DEVICE=/dev/null (gs-metrics-manager won't report GPU usage)."; \
		fi; \
		NPU_DEV=$$(ls -1 /dev/accel/accel* 2>/dev/null | head -1); \
		if [ -n "$$NPU_DEV" ]; then \
			echo "✅ NPU detected: NPU_DEVICE=$$NPU_DEV"; \
			sed -i "s|^NPU_DEVICE=.*|NPU_DEVICE=$$NPU_DEV|" .env; \
		else \
			echo "⚠  No Intel NPU found at /dev/accel/ — leaving NPU_DEVICE=/dev/null (gs-metrics-manager won't report NPU usage)."; \
		fi; \
	fi

build: ## Force a rebuild of the viz-backend image (up and up-vss rebuild it only when missing or backend/ changed)
	docker compose -f docker-compose.yml build

BACKEND_IMAGE = uav-ground-station-backend:local

# Builds viz-backend only when the image is missing or a file in backend/ is
# newer than it, so a start with unchanged code never needs the network.
_build_if_changed:
	@created=$$(docker image inspect -f '{{.Created}}' $(BACKEND_IMAGE) 2>/dev/null); \
	if [ -z "$$created" ]; then \
		echo "[build] $(BACKEND_IMAGE) not found -- building"; \
		docker compose -f docker-compose.yml build; \
	else \
		built=$$(date -d "$$created" +%s); \
		newest=$$(find backend -type f -not -path '*/__pycache__/*' -printf '%T@\n' | sort -n | tail -1 | cut -d. -f1); \
		if [ "$$newest" -gt "$$built" ]; then \
			echo "[build] backend/ changed since the image was built -- rebuilding"; \
			docker compose -f docker-compose.yml build; \
		else \
			echo "[build] $(BACKEND_IMAGE) is up to date -- skipping build"; \
		fi; \
	fi

up: init _build_if_changed ## Start the ground-station viewer + mediamtx relay sidecar (H.264 Relay mode); builds viz-backend only if missing or changed
	docker compose -f docker-compose.yml -f docker-compose-mediamtx.yml up -d --no-build

down: ## Stop and remove the ground-station viewer + mediamtx relay sidecar
	docker compose -f docker-compose.yml -f docker-compose-mediamtx.yml down -v

VSS_COMPOSE = docker compose -f docker-compose.yml -f docker-compose-mediamtx.yml -f docker-compose-vss.yml

VSS_REQUIRED_VARS = VSS_MINIO_USER VSS_MINIO_PASSWORD VSS_POSTGRES_USER VSS_POSTGRES_PASSWORD VSS_EMBEDDING_MODEL DATAPREP_DETECTION_MODEL

# One message listing everything missing, instead of one compose error per use.
_check_vss_env:
	@missing=""; \
	for v in $(VSS_REQUIRED_VARS); do \
		eval "val=\$$$$v"; \
		[ -n "$$val" ] || missing="$$missing $$v"; \
	done; \
	if [ -n "$$missing" ]; then \
		echo "ERROR: set these in .env before make up-vss (see .env.example):$$missing" >&2; \
		exit 1; \
	fi

up-vss: init _check_vss_env _build_if_changed ## Relay mode + VSS search containers (enables Live Natural Language Search)
	$(VSS_COMPOSE) up -d --no-build
	@echo ""; \
	echo "✅ UAV ground station is up. Open the dashboard at: http://$(VIZ_HOST_IP):$(or $(VIZ_PORT),8080)"

down-vss: ## Stop the relay + VSS stack, keeping indexed footage/embeddings
	$(VSS_COMPOSE) down

# Index, footage and their metadata must be cleared together to stay consistent;
# the downloaded CLIP/YOLOX models (vss-ov-models, vss-yolox-models) are kept.
VSS_INDEX_VOLUMES = vss-vdms-db vss-minio-data vss-pg-data vss-data-prep

clean-vss-index: ## Stop the relay + VSS stack and DELETE indexed footage/embeddings, keeping models
	$(VSS_COMPOSE) down
	@project=$$($(VSS_COMPOSE) config | awk '/^name:/{print $$2; exit}'); \
	for v in $(VSS_INDEX_VOLUMES); do \
		docker volume rm -f "$${project}_$$v" >/dev/null && echo "removed $${project}_$$v"; \
	done

delete-vss-volumes: ## DELETE ALL VSS volumes incl. downloaded models (asks first; FORCE=1 skips)
	@if [ "$(FORCE)" != "1" ]; then \
		printf "This deletes ALL VSS volumes, including ~1.6 GB of downloaded models. Continue? [y/N] "; \
		read ans; case "$$ans" in [yY]|[yY][eE][sS]) ;; *) echo "Aborted."; exit 1;; esac; \
	fi
	$(VSS_COMPOSE) down -v

logs: ## Tail logs from all running containers
	docker compose -f docker-compose.yml -f docker-compose-mediamtx.yml logs -f

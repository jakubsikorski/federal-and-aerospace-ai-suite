# Handheld multimodal application - operator entry points.
# Design: docs/design/APPLICATION_DESIGN.md

COMPOSE := docker compose -f compose/bootstrap-compose.yaml
CAMERAS_COMPOSE := docker compose -f compose/cameras.yaml
# How many demo cameras to publish. Real cameras replace these entirely.
CAMERAS ?= 1
TLS_DIR := config/tls
# LAN address the tablet actually connects to. Both a DNS name and an IP SAN are
# issued because tablets normally reach the device by address (design 15.4).
HOST_NAME ?= $(shell hostname)
HOST_IP   ?= $(shell ip -4 -o addr show scope global | awk 'NR==1{split($$4,a,"/"); print a[1]}')

.PHONY: tls demo-up demo-down health cameras cameras-down test

# Make's dependency rule is the idempotency: regenerating only happens if the
# cert is missing. No script, no state file.
tls: $(TLS_DIR)/server.crt

$(TLS_DIR)/server.crt:
	@mkdir -p $(TLS_DIR)
	openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
	  -keyout $(TLS_DIR)/server.key -out $(TLS_DIR)/server.crt \
	  -subj "/CN=$(HOST_NAME)" \
	  -addext "subjectAltName=DNS:$(HOST_NAME),DNS:localhost,IP:$(HOST_IP),IP:127.0.0.1"
	@chmod 600 $(TLS_DIR)/server.key
	@echo "Trust this certificate on the tablet (fingerprint below), then browse to https://$(HOST_IP):8443"
	@openssl x509 -in $(TLS_DIR)/server.crt -noout -fingerprint -sha256

demo-up: tls
	$(COMPOSE) up -d --build

demo-down: cameras-down
	$(COMPOSE) down

# Demo feeds only (design section 22: removable demo assets).
cameras:
	$(CAMERAS_COMPOSE) up -d $(foreach i,$(shell seq 1 $(CAMERAS)),cam$(i))

cameras-down:
	-$(CAMERAS_COMPOSE) down

health:
	@curl -sk https://127.0.0.1:8443/api/health | python3 -m json.tool

# Runs inside the image so the self-check sees the same dependencies as the service.
test:
	$(COMPOSE) exec backend python3 test_backend.py

# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Federal Aerospace packaging — orchestration only.
#
# Component-specific staging logic lives in each component's own Makefile.
# This Makefile is the single source of truth for local and CI builds.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := all

# --- Configuration -----------------------------------------------------------

DIST_DIR  ?= dist
BUILD_DIR ?= build
ROOT      := $(abspath .)

# Source directories for each packaged component.
HANDHELD_SRC := handheld-multi-modal
DTD_SRC      := deterministic-threat-detection
UAVSDK_SRC   := uav-mission-compute-sdk
UAVVA_SRC    := uav-vision-analytics

# Output archives.
HANDHELD_ZIP := $(DIST_DIR)/handheld-multi-modal.zip
DTD_ZIP      := $(DIST_DIR)/deterministic-threat-detection.zip
UAV_ZIP      := $(DIST_DIR)/uav-mission-apps.zip
SOLUTION_ZIP := $(DIST_DIR)/federal-and-aerospace-ai-suite.zip

# --- Public targets ----------------------------------------------------------

.PHONY: all
all: handheld-multi-modal deterministic-threat-detection uav-mission-apps federal-and-aerospace-ai-suite ## Build every component ZIP

.PHONY: handheld-multi-modal
handheld-multi-modal: $(HANDHELD_ZIP) ## Build handheld-multi-modal.zip

.PHONY: deterministic-threat-detection
deterministic-threat-detection: $(DTD_ZIP) ## Build deterministic-threat-detection.zip

.PHONY: uav-mission-apps
uav-mission-apps: $(UAV_ZIP) ## Build uav-mission-apps.zip (uav-mission-compute-sdk + uav-vision-analytics)

.PHONY: federal-and-aerospace-ai-suite
federal-and-aerospace-ai-suite: $(SOLUTION_ZIP) ## Build federal-and-aerospace-ai-suite.zip with the entire solution

.PHONY: clean
clean: ## Remove build and dist directories
	rm -rf "$(BUILD_DIR)" "$(DIST_DIR)"

.PHONY: help
help: ## Show this help
	@awk 'BEGIN{FS=":.*##"} /^[a-zA-Z_-]+:.*##/ {printf "  %-32s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# --- Simple component ZIPs (just zip the source directory) -------------------

# Generated per-device files (models, history, TLS keys, detected settings) are
# excluded so a ZIP built on a deployed checkout never ships device state.
HANDHELD_EXCLUDES := 'handheld-multi-modal/data/*' 'handheld-multi-modal/datasets/*' \
	'handheld-multi-modal/config/tls/*' 'handheld-multi-modal/config/devices.yaml' \
	'handheld-multi-modal/config/mediamtx.yml' 'handheld-multi-modal/docs/application/*' \
	'handheld-multi-modal/.env' 'handheld-multi-modal/.demo-assets.lock'

$(HANDHELD_ZIP): $(shell find $(HANDHELD_SRC) -type f 2>/dev/null)
	@mkdir -p "$(DIST_DIR)"
	@rm -f "$@"
	zip -rq "$@" "$(HANDHELD_SRC)" -x $(HANDHELD_EXCLUDES)
	@echo "Archive ready: $@"

$(DTD_ZIP): $(shell find $(DTD_SRC) -type f 2>/dev/null)
	@mkdir -p "$(DIST_DIR)"
	@rm -f "$@"
	zip -rq "$@" "$(DTD_SRC)"
	@echo "Archive ready: $@"

$(UAV_ZIP): $(shell find $(UAVSDK_SRC) $(UAVVA_SRC) -type f 2>/dev/null)
	@mkdir -p "$(DIST_DIR)"
	@rm -f "$@"
	zip -rq "$@" "$(UAVSDK_SRC)" "$(UAVVA_SRC)"
	@echo "Archive ready: $@"

# --- Full solution ZIP -------------------------------------------------------
#
# Bundles the entire federal-and-aerospace-ai-suite tree.

SOLUTION_STAGE := $(BUILD_DIR)/federal-and-aerospace-ai-suite

$(SOLUTION_ZIP): \
		$(shell find $(HANDHELD_SRC) $(DTD_SRC) $(UAVSDK_SRC) $(UAVVA_SRC) docs -type f 2>/dev/null) \
		README.md
	@echo ">> Staging federal-and-aerospace-ai-suite (full solution)"
	@rm -rf "$(SOLUTION_STAGE)"
	@mkdir -p "$(SOLUTION_STAGE)/federal-and-aerospace-ai-suite"
	rsync -a \
		--exclude='build' --exclude='dist' --exclude='.git' \
		--exclude='handheld-multi-modal/data' \
		--exclude='handheld-multi-modal/datasets' \
		--exclude='handheld-multi-modal/config/tls' \
		--exclude='handheld-multi-modal/config/devices.yaml' \
		--exclude='handheld-multi-modal/config/mediamtx.yml' \
		--exclude='handheld-multi-modal/docs/application' \
		--exclude='handheld-multi-modal/.env' \
		--exclude='handheld-multi-modal/.demo-assets.lock' \
		./ "$(SOLUTION_STAGE)/federal-and-aerospace-ai-suite/"
	@mkdir -p "$(DIST_DIR)"
	@rm -f "$@"
	cd "$(SOLUTION_STAGE)" && zip -rq "$(abspath $@)" federal-and-aerospace-ai-suite
	@echo "Archive ready: $@"

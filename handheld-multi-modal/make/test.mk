# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

# Test targets, included from ../Makefile (uses its SHELL and COMPOSE).
# Every test runs in a container; the host needs only Docker and Make.
#   make test-all    offline: no started application, models or network
#   make test-live   needs the started application (make up or make demo-up)

# TEST_VERBOSE=1 shows every test's output, not only that of failed tests.
export TEST_VERBOSE ?=
COVERAGE_MIN ?= 75
COVERAGE_OUTPUT ?=
FUZZ_SEED ?= 504
FUZZ_MUTATIONS ?= 32
# Image for `make test` and `make test-fuzz`; the deployed Backend by default.
TEST_IMAGE ?= handheld/backend:dev

# Backend Dockerfile `test` stage: dependencies, coverage, Firefox, geckodriver.
OFFLINE_IMAGE := handheld/backend:test
# The DLSPS image the application runs, read from the Compose file.
DLSPS_IMAGE := $(shell awk '/^  dlsps:/{f=1} f&&/image:/{print $$2; exit}' compose/handheld-multi-modal.yaml)

# Prints one PASS/FAIL line per target, then the output if the test failed or
# TEST_VERBOSE=1; see make/run-test.sh.
RUN_TEST = bash make/run-test.sh $@

# Non-root, no network, read-only root file system, no capabilities.
SANDBOX := docker run --rm \
  --user "$$(id -u):$$(id -g)" \
  --network none \
  --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,size=512m \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  -e PYTHONDONTWRITEBYTECODE=1

.PHONY: test-all test-live test-image test-coverage test-crops test-assets test-ui \
  test test-fuzz test-history test-crops-live

test-all: test-coverage test-crops test-assets test-ui

test-live: test-history test-crops-live

# --- Offline ----------------------------------------------------------------

test-image:
	@$(RUN_TEST) docker build -q --target test -t $(OFFLINE_IMAGE) backend

# Backend checks with a line coverage gate. COVERAGE_OUTPUT=<dir> adds coverage.xml,
# written before the gate so it is kept when coverage is below the minimum.
test-coverage: test-image
	@$(if $(COVERAGE_OUTPUT),mkdir -p "$(COVERAGE_OUTPUT)")
	$(RUN_TEST) $(SANDBOX) \
	  -e COVERAGE_FILE=/tmp/.coverage \
	  -v "$(CURDIR):/srv:ro" \
	  $(if $(COVERAGE_OUTPUT),-v "$(abspath $(COVERAGE_OUTPUT)):/coverage") \
	  -w /srv/backend \
	  $(OFFLINE_IMAGE) sh -c ' \
	    coverage run --source=app test_backend.py && \
	    { [ ! -d /coverage ] || coverage xml -q -o /coverage/coverage.xml; } && \
	    coverage report --skip-empty --fail-under=$(COVERAGE_MIN)'

# Crop producer checks in the DLSPS image, without the application or an NPU.
test-crops:
	@$(RUN_TEST) $(SANDBOX) \
	  -e CROP_TEST_TMPDIR=/tmp/crop-test \
	  -v "$(CURDIR)/scripts/dlsps:/opt/handheld-crops:ro" \
	  -w /opt/handheld-crops \
	  --entrypoint python3 \
	  $(DLSPS_IMAGE) test_crops.py

# Asset preparation and Make dry-run checks.
test-assets: test-image
	@$(RUN_TEST) $(SANDBOX) \
	  -v "$(CURDIR):/srv:ro" \
	  -w /srv \
	  $(OFFLINE_IMAGE) python3 scripts/test_demo_assets.py

# Browser checks in headless Firefox, which needs the larger /dev/shm.
test-ui: test-image
	@$(RUN_TEST) $(SANDBOX) \
	  --shm-size 1g \
	  -v "$(CURDIR):/srv:ro" \
	  -w /srv \
	  $(OFFLINE_IMAGE) python3 backend/test_ui.py

# Backend checks in the deployed Backend image, without coverage.
test:
	@$(RUN_TEST) docker run --rm \
	  --network none \
	  -v "$(CURDIR):/srv" \
	  -w /srv/backend \
	  $(TEST_IMAGE) python3 test_backend.py

# REST API fuzzing (backend/FUZZING.md); not part of test-all.
test-fuzz:
	@mkdir -p docs/application/evidence/SDL504-api-fuzz
	$(RUN_TEST) $(SANDBOX) \
	  --memory 1g --cpus 2 --pids-limit 128 \
	  -v "$(CURDIR):/srv:ro" \
	  -v "$(CURDIR)/docs/application/evidence/SDL504-api-fuzz:/evidence" \
	  -w /srv/backend \
	  $(TEST_IMAGE) python3 fuzz_api.py \
	    --output /evidence --seed "$(FUZZ_SEED)" --mutations "$(FUZZ_MUTATIONS)"

# --- Against the started application ----------------------------------------

# History queries against the demo cameras.
test-history:
	@$(RUN_TEST) docker run --rm \
	  --network host \
	  --user "$$(id -u):$$(id -g)" \
	  -e ASSET_ROOT=/srv \
	  -e NO_PROXY='*' -e no_proxy='*' \
	  -v "$(CURDIR):/srv" \
	  -w /srv \
	  handheld/backend:dev python3 scripts/test_history.py

# Real NPU and VA crop path inside the running DLSPS service.
test-crops-live:
	@$(RUN_TEST) $(COMPOSE) exec -T dlsps \
	  python3 /opt/handheld-crops/test_crops.py --live --output /tmp/crop-live

# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

# Test targets, included from ../Makefile (uses its SHELL, .ONESHELL and COMPOSE).
#
# Every test runs in a container, so the host needs only Docker and Make.
#   make test-all    offline suite; no started application, models or network
#   make test-live   needs the started application (make up or make demo-up)

# --- Settings ----------------------------------------------------------------

COVERAGE_MIN ?= 75
COVERAGE_OUTPUT ?=
# Image for `make test` and `make test-fuzz`; the deployed Backend by default.
TEST_IMAGE ?= handheld/backend:dev

# Built from the Backend Dockerfile's `test` stage: Python dependencies,
# coverage, Make, headless Firefox ESR and a pinned geckodriver.
OFFLINE_IMAGE := handheld/backend:test
# The same DLSPS image the application runs, read from the Compose file.
DLSPS_IMAGE := $(shell awk '/^  dlsps:/{f=1} f&&/image:/{print $$2; exit}' compose/handheld-multi-modal.yaml)

# Non-root, no network, read-only root file system, no capabilities.
SANDBOX := docker run --rm \
  --user "$$(id -u):$$(id -g)" \
  --network none \
  --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,size=512m \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  -e PYTHONDONTWRITEBYTECODE=1

# --- Result lines --------------------------------------------------------------
# `run_test <label> <command...>` prints one line such as
#   test-coverage    PASS  25s  118 checks, line coverage 92% (minimum 75%)
# The command's output, including the failure-path logs the checks provoke on
# purpose, is printed only when it fails, or always with TEST_VERBOSE=1.
define run_test
run_test() {
  local label=$$1; shift
  if [[ $${TEST_VERBOSE:-} == 1 ]]; then "$$@"; return; fi

  local log rc=0 start=$$SECONDS
  log=$$(mktemp)
  trap 'rm -f "$$log"' RETURN
  printf '%-16s ' "$$label"
  "$$@" >"$$log" 2>&1 || rc=$$?

  # Summary from the output: checks passed, coverage total, fuzz findings.
  local checks coverage findings summary=()
  checks=$$(grep -cE '^(ok test_|PASS)' "$$log" || true)
  coverage=$$(awk '/^TOTAL/ {print $$NF}' "$$log")
  findings=$$(grep -oE '"findings": [0-9]+' "$$log" | tail -1 | grep -oE '[0-9]+$$' || true)
  ((checks == 1)) && summary+=("1 check")
  ((checks > 1)) && summary+=("$$checks checks")
  [[ -n $$coverage ]] && summary+=("line coverage $$coverage (minimum $(COVERAGE_MIN)%)")
  [[ -n $$findings ]] && summary+=("$$findings findings")

  local elapsed=$$((SECONDS - start))s details='' item
  for item in "$${summary[@]}"; do details+="$${details:+, }$$item"; done
  if ((rc == 0)); then
    echo "PASS  $$elapsed$${details:+  $$details}"
  else
    echo "FAIL  $$elapsed (exit $$rc); output:"
    cat "$$log"
  fi
  return $$rc
}
endef

# --- Suites --------------------------------------------------------------------

test-all: test-coverage test-crops test-assets test-ui

test-live: test-history test-crops-live

# --- Offline tests -----------------------------------------------------------

test-image:
	@$(run_test)
	run_test test-image docker build -q --target test -t $(OFFLINE_IMAGE) backend

# Backend checks with a line coverage gate; COVERAGE_OUTPUT=<dir> adds coverage.xml.
test-coverage: test-image
	@$(run_test)
	$(if $(COVERAGE_OUTPUT),mkdir -p "$(COVERAGE_OUTPUT)")
	run_test test-coverage $(SANDBOX) \
	  -e COVERAGE_FILE=/tmp/.coverage \
	  -v "$(CURDIR):/srv:ro" \
	  $(if $(COVERAGE_OUTPUT),-v "$(abspath $(COVERAGE_OUTPUT)):/coverage") \
	  -w /srv/backend \
	  $(OFFLINE_IMAGE) sh -c ' \
	    coverage run --source=app test_backend.py && \
	    coverage report --skip-empty --fail-under=$(COVERAGE_MIN) && \
	    if [ -d /coverage ]; then coverage xml -q -o /coverage/coverage.xml; fi'

# Crop producer checks in the DLSPS image, without the application or an NPU.
test-crops:
	@$(run_test)
	run_test test-crops $(SANDBOX) \
	  -e CROP_TEST_TMPDIR=/tmp/crop-test \
	  -v "$(CURDIR)/scripts/dlsps:/opt/handheld-crops:ro" \
	  -w /opt/handheld-crops \
	  --entrypoint python3 \
	  $(DLSPS_IMAGE) test_crops.py

# Asset preparation and Make dry-run checks.
test-assets: test-image
	@$(run_test)
	run_test test-assets $(SANDBOX) \
	  -v "$(CURDIR):/srv:ro" \
	  -w /srv \
	  $(OFFLINE_IMAGE) python3 scripts/test_demo_assets.py

# Browser checks in headless Firefox; Firefox needs the larger /dev/shm.
test-ui: test-image
	@$(run_test)
	run_test test-ui $(SANDBOX) \
	  --shm-size 1g \
	  -v "$(CURDIR):/srv:ro" \
	  -w /srv \
	  $(OFFLINE_IMAGE) python3 backend/test_ui.py

# Backend checks in the deployed Backend image, without coverage.
test:
	@$(run_test)
	run_test test docker run --rm \
	  --network none \
	  -v "$(CURDIR):/srv" \
	  -w /srv/backend \
	  $(TEST_IMAGE) python3 test_backend.py

# REST API fuzzing (backend/FUZZING.md); not part of test-all.
.PHONY: test-fuzz
test-fuzz:
	@$(run_test)
	mkdir -p docs/application/evidence/SDL504-api-fuzz
	run_test test-fuzz $(SANDBOX) \
	  --memory 1g --cpus 2 --pids-limit 128 \
	  -v "$(CURDIR):/srv:ro" \
	  -v "$(CURDIR)/docs/application/evidence/SDL504-api-fuzz:/evidence" \
	  -w /srv/backend \
	  $(TEST_IMAGE) python3 fuzz_api.py \
	    --output /evidence \
	    --seed "$(or $(FUZZ_SEED),504)" \
	    --mutations "$(or $(FUZZ_MUTATIONS),32)"

# --- Tests against the started application ----------------------------------

# History queries against the demo cameras.
test-history:
	@$(run_test)
	run_test test-history docker run --rm \
	  --network host \
	  --user "$$(id -u):$$(id -g)" \
	  -e ASSET_ROOT=/srv \
	  -e NO_PROXY='*' -e no_proxy='*' \
	  -v "$(CURDIR):/srv" \
	  -w /srv \
	  handheld/backend:dev python3 scripts/test_history.py

# Real NPU and VA crop path inside the running DLSPS service.
test-crops-live:
	@$(run_test)
	run_test test-crops-live $(COMPOSE) exec -T dlsps \
	  python3 /opt/handheld-crops/test_crops.py --live --output /tmp/crop-live

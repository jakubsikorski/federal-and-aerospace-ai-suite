# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
# shellcheck shell=bash

# Startup timing helpers for Bash scripts and Make recipes. Source this file;
# it does not change shell options. Every helper is a no-op unless the shared
# runner started the command (it sets STARTUP_TIMING_DIR and STARTUP_TIMING_CONFIG).
STARTUP_TIMING_CORE="${BASH_SOURCE[0]%/*}/startup_timing_core.py"

# timing_begin <group> <label>: prints a token for timing_end.
timing_begin() {
  if [[ -n "${STARTUP_TIMING_DIR:-}" ]]; then python3 "$STARTUP_TIMING_CORE" begin "$@"; fi
}

# timing_end <token> [--code N] [--status ok|failed|cached|reused]
timing_end() {
  if [[ -n "${1:-}" ]]; then python3 "$STARTUP_TIMING_CORE" end "$@"; fi
}

# timing_cached <group> <label>: records work that was skipped because it was cached.
timing_cached() {
  timing_end "$(timing_begin "$@")" --status cached
}

# timed <group> <label> <command...>: runs the command and records its duration.
timed() {
  local group=$1 label=$2 token code=0
  shift 2
  token=$(timing_begin "$group" "$label")
  "$@" || code=$?
  timing_end "$token" --code "$code"
  return "$code"
}

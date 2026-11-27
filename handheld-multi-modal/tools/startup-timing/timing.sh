#!/usr/bin/env bash
# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
# shellcheck shell=bash

# Startup timing. Two ways to use this file:
#   tools/startup-timing/timing.sh run -- <command>      run a command and write a report
#   source tools/startup-timing/timing.sh                 helpers for scripts and Make recipes
# The helpers do nothing unless the command was started by `timing.sh run`, and sourcing
# does not change shell options. Group names a report section; label is the row text.
# Optional --service NAME (before the group) tags a step with the service that owns it.
# The helpers are best-effort: a timing fault never fails or aborts the application.
# `timed` is for external commands. A shell function run through it loses `set -e` inside
# the function; wrap a function with timing_begin / timing_end around the call instead.
TIMING_PY="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/timing.py"

# timing_begin [--service NAME] <group> <label>: prints a token for timing_end.
timing_begin() {
  local options=()
  while [[ ${1:-} == --service || ${1:-} == --parent ]]; do options+=("$1" "${2:-}"); shift $(($# > 1 ? 2 : 1)); done
  if [[ -n "${STARTUP_TIMING_DIR:-}" ]]; then python3 "$TIMING_PY" begin "${options[@]}" -- "$@" || true; fi
}

# timing_end <token> [--code N] [--status ok|failed|cached|reused]
timing_end() {
  if [[ -n "${1:-}" ]]; then python3 "$TIMING_PY" end "$@" || true; fi
}

# timing_cached [--service NAME] <group> <label>: records work skipped because it was cached.
timing_cached() {
  timing_end "$(timing_begin "$@")" --status cached
}

# timed [--service NAME] <group> <label> <external command...>: runs the command and records
# its duration. Steps timed inside the command are nested under this one in the report.
timed() {
  local options=() token code=0
  while [[ ${1:-} == --service ]]; do options+=("$1" "$2"); shift 2; done
  local group=$1 label=$2
  shift 2
  token=$(timing_begin "${options[@]}" "$group" "$label") || token=''
  STARTUP_TIMING_PARENT="$token" "$@" || code=$?
  timing_end "$token" --code "$code"
  return "$code"
}

# Executed rather than sourced: forward to the CLI (run, render, compare, ...).
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  exec python3 "$TIMING_PY" "$@"
fi

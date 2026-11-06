#!/usr/bin/env bash
# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

# Usage: run-test.sh <label> <command...>
# Runs the command and prints one result line, e.g. "test-ui          PASS  68s".
# The command's output is shown after the result line when it fails, or always
# with TEST_VERBOSE=1.
set -euo pipefail

label=$1
shift
output=$(mktemp)
trap 'rm -f "$output"' EXIT

printf '%-16s ' "$label"
start=$SECONDS
status=0
"$@" >"$output" 2>&1 || status=$?
elapsed=$((SECONDS - start))

if ((status == 0)); then
  echo "PASS  ${elapsed}s"
else
  echo "FAIL  ${elapsed}s (exit $status)"
fi

if ((status != 0)) || [[ ${TEST_VERBOSE:-} == 1 ]]; then
  cat "$output"
fi

exit "$status"

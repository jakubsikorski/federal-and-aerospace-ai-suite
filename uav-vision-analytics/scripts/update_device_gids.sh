#!/usr/bin/env bash
# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Write the numeric group IDs that own the GPU, NPU and camera device nodes into .env
# (GPU_CARD_GID, GPU_RENDER_GID, NPU_GID, CAMERA_GID). The compose files pass them to
# the pipeline-server container through group_add, so access works on any host OS,
# where the render/video group numbers differ.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
ENV_FILE="${ENV_FILE:-.env}"
[ -f "$ENV_FILE" ] || { echo "ERROR: $ENV_FILE not found - run 'make init' first." >&2; exit 1; }

env_get() { grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true; }

env_set() {
  if grep -q "^$1=" "$ENV_FILE"; then
    sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"
  else
    echo "$1=$2" >> "$ENV_FILE"
  fi
}

# Print the group ID of a device node; nothing for a missing node or a root-owned one
# (/dev/null when the NPU is disabled), so the container never joins group 0.
gid_of() {
  local dev="$1" gid
  [ -n "$dev" ] && [ -e "$dev" ] || return 0
  gid="$(stat -c %g "$dev")"
  [ "$gid" != "0" ] && echo "$gid" || true
}

gpu_card="$(env_get GPU_DEVICE)"
gpu_render="$(env_get GPU_RENDER_DEVICE)"
npu="$(env_get NPU_DEVICE)"
camera="$(env_get CAMERA_DEVICE_1)"

# Compose rejects duplicate group_add entries, and the GPU, NPU and camera nodes often share a
# group. A repeated or missing ID is replaced by an unused placeholder (65501+) that grants nothing.
seen=" "
spare=65501
set_unique() {
  local key="$1" gid="$2"
  if [ -z "$gid" ] || [[ "$seen" == *" $gid "* ]]; then
    gid="$spare"; spare=$((spare + 1))
  fi
  seen+="$gid "
  env_set "$key" "$gid"
}

set_unique GPU_RENDER_GID "$(gid_of "${gpu_render:-/dev/dri/renderD128}")"
set_unique GPU_CARD_GID "$(gid_of "${gpu_card:-/dev/dri/card0}")"
set_unique NPU_GID "$(gid_of "$npu")"
set_unique CAMERA_GID "$(gid_of "$camera")"

echo "[gids] card=$(env_get GPU_CARD_GID) render=$(env_get GPU_RENDER_GID) npu=$(env_get NPU_GID) camera=$(env_get CAMERA_GID)"

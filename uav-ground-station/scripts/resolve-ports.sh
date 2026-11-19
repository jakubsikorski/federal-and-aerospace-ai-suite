#!/usr/bin/env bash
# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Usage: resolve-ports.sh VAR[+VAR2]:DEFAULT ...
#
# Moves each host port in .env that another process already listens on to the next
# free port. When the UAV Mission Compute SDK has containers on this machine, its
# published ports (8080, 8554, 8888, 8889) also count as taken, even while the SDK is
# stopped or not yet started: if the ground station held one of them, the SDK's MediaMTX
# could not publish its ports and the UAV cameras would have no RTSP source.
# Ports published by this stack's own containers are left alone, so running `make up`
# again changes nothing. `VAR+VAR2` keeps two variables (such as the WebRTC ICE UDP and
# TCP ports) on one port number. ENV_FILE overrides the file to edit (default: .env).
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE=${ENV_FILE:-.env}
[ -f "$ENV_FILE" ] || exit 0

SDK_PORTS=" 8080 8554 8888 8889 "
sdk_here=""
if [ -n "$(docker ps -aq --filter name='^px4-gazebo$' --filter name='^mediamtx$' 2>/dev/null)" ]; then
  sdk_here=1
fi
ours=$(docker ps --filter name=uav-gs- --format '{{.Ports}}' 2>/dev/null || true)
chosen=" "

in_use() { [ -n "$(ss -H -ltun "sport = :$1" 2>/dev/null)" ]; }
is_ours() { grep -q ":$1->" <<<"$ours"; }
reserved() { [ -n "$sdk_here" ] && [[ "$SDK_PORTS" == *" $1 "* ]]; }
taken() {
  reserved "$1" && return 0
  in_use "$1" && ! is_ours "$1" && return 0
  [[ "$chosen" == *" $1 "* ]]
}
current() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -1; }
store() {
  if grep -q "^$1=" "$ENV_FILE"; then sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"; else echo "$1=$2" >>"$ENV_FILE"; fi
}

for spec in "$@"; do
  names=${spec%%:*}
  first=${names%%+*}
  port=$(current "$first")
  port=${port:-${spec##*:}}
  start=$port
  while taken "$port"; do
    port=$((port + 1))
    if [ "$port" -gt 65535 ] || [ $((port - start)) -gt 100 ]; then
      echo "ERROR: no free port found from $start for $names" >&2
      exit 1
    fi
  done
  chosen+="$port "
  IFS=+ read -ra vars <<<"$names"
  for var in "${vars[@]}"; do store "$var" "$port"; done
  if [ "$port" != "$start" ]; then
    echo "[ports] $names: $start is taken on this machine -- using $port (saved in $ENV_FILE)"
  fi
done

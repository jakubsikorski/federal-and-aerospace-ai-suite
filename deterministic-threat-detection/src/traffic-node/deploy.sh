#!/usr/bin/env bash
# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Start / stop machine-4's containers (metrics-manager + load agent) from machine-1.
#   ./deploy.sh up | down | status
#
# Uses machine-1's docker client against machine-4's engine over ssh (DOCKER_HOST=ssh://...), so machine-4
# needs neither a compose plugin nor a copy of these files. The agent image is built here and copied over.
# Addresses come from ../../lab.env (SSH_USER, M1_NOISE_IP, M4_LAB_IP, M4_NOISE_IP).
# Overrides: NODE_HOST=user@host  METRICS_PORT=19090  LOAD_PORT=8099  MAX_MBPS=1000
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
. "$ROOT/src/lib/label.sh"
NODE_HOST=${NODE_HOST:-${SSH_USER:+${M4_LAB_IP:+$SSH_USER@$M4_LAB_IP}}}
[ -n "$NODE_HOST" ] || { echo "traffic-node: no host (set SSH_USER and M4_LAB_IP in lab.env); skipping"; exit 0; }
: "${M1_NOISE_IP:?set M1_NOISE_IP in lab.env}" "${M4_NOISE_IP:?set M4_NOISE_IP in lab.env}"

export DOCKER_HOST=ssh://$NODE_HOST
export DOCKER_SSH_OPTS="-o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new"
export LOAD_BIND=${M4_LAB_IP:?} SERVER_IP=$M1_NOISE_IP CLIENT_IP=$M4_NOISE_IP
export METRICS_PORT=${METRICS_PORT:-19090} LOAD_PORT=${LOAD_PORT:-8099} MAX_MBPS=${MAX_MBPS:-1000}
COMPOSE=(docker compose -f "$HERE/docker-compose.yml")

case "${1:-}" in
  up)
    echo "[$(label "$NODE_HOST")] building and copying the load agent image"
    DOCKER_HOST= docker build -q -t tsn-load-agent:local "$HERE/agent" >/dev/null
    DOCKER_HOST= docker save tsn-load-agent:local | docker load >/dev/null
    # containers of the earlier hand-started / docker-run versions would hold the metrics port
    docker rm -f metrics-manager tsn-metrics-manager >/dev/null 2>&1 || true
    "${COMPOSE[@]}" up -d 2>&1 | tail -3 | sed "s/^/[$(label "$NODE_HOST")] /"
    ;;
  down) "${COMPOSE[@]}" down -t 2 2>&1 | tail -2 | sed "s/^/[$(label "$NODE_HOST")] /" ;;
  status) echo "== $(label "$NODE_HOST")"; "${COMPOSE[@]}" ps -a --format 'table {{.Service}}\t{{.Status}}' ;;
  *) sed -n '4,6p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac

# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Sourced by the deploy scripts. `label <host>` prints "machine-N" (plus the host) for log prefixes, using the
# addresses in lab.env (M<N>_LAB_IP / M<N>_TSN_IP), or the host unchanged if it is not a known machine.
_LABEL_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
[ ! -f "$_LABEL_ROOT/lab.env" ] || { set -a; . "$_LABEL_ROOT/lab.env"; set +a; }

label() {
  local host=$1 addr=${1##*@} n var
  for n in 1 2 3 4; do
    for var in M${n}_LAB_IP M${n}_TSN_IP; do
      [ "${!var:-}" = "$addr" ] && { echo "machine-$n $host"; return; }
    done
  done
  case "$addr" in tsn-machine-[0-9]) echo "machine-${addr##*-} $host"; return ;; esac
  echo "$host"
}

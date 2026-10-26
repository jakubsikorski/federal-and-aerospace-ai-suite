#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$root"
exec "${SMOKE_PYTHON:-python3}" scripts/smoke/chatqna.py rag

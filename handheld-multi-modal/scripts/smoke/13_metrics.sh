#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${METRICS_URL:-}" || -z "${METRICS_TELEGRAF_URL:-}" ]]; then
  echo "BLOCKED: set METRICS_URL (API port 9090) and METRICS_TELEGRAF_URL (collector port 9273)." >&2
  echo "Both must reference the same operator-started Metrics Manager instance." >&2
  echo "Scope: live CPU/RAM collector data only; no GPU/NPU/power or Panther Lake pass." >&2
  exit 2
fi
python="${SMOKE_PYTHON:-python3}"
command -v "$python" >/dev/null || {
  echo "BLOCKED: missing $python; this smoke installs nothing." >&2
  exit 2
}

exec "$python" - <<'PY'
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

base = os.environ["METRICS_URL"].rstrip("/")
collector = os.environ["METRICS_TELEGRAF_URL"].rstrip("/")
for endpoint in (base, collector):
    try:
        parsed = urllib.parse.urlsplit(endpoint)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("invalid base")
    except ValueError:
        print("BLOCKED: URLs must be HTTP(S) bases without credentials/query/fragment.",
              file=sys.stderr)
        sys.exit(2)

names = (
    "cpu_usage_user", "cpu_usage_system", "cpu_usage_idle",
    "mem_used_percent", "mem_available_percent", "mem_total", "mem_used",
)


def get(endpoint, path):
    request = urllib.request.Request(endpoint + path)
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise ValueError("API response exceeds 1 MiB limit")
        return body.decode("utf-8")


def sample():
    rows = {}
    for line in get(collector, "/metrics").splitlines():
        match = re.match(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{|[ \t])", line)
        if not match or match[1] not in names:
            continue
        name = match[1]
        parts = line.rsplit(None, 2)
        if len(parts) != 3:
            raise ValueError(f"{name}: missing original Prometheus export timestamp")
        series, value_text, timestamp_text = parts
        tags = {}
        for label in ("host", "cpu"):
            label_match = re.search(r'(?:\{|,)' + label + r'="((?:\\.|[^"\\])*)"', series)
            if label_match:
                tags[label] = json.loads('"' + label_match[1] + '"')
        rows.setdefault(name, []).append({
            "tags": tags, "value": float(value_text),
            "timestamp": float(timestamp_text) * 1e6,
        })
    result = {}
    for name in names:
        matches = rows.get(name, [])
        if len(matches) != 1:
            raise ValueError(f"{name}: require exactly one series, found {len(matches)}")
        row = matches[0]
        tags = row.get("tags")
        if not isinstance(tags, dict) or not tags.get("host"):
            raise ValueError(f"{name}: missing collector host label")
        if name.startswith("cpu_") and tags.get("cpu") != "cpu-total":
            raise ValueError(f"{name}: expected aggregate cpu-total")
        value = row.get("value")
        timestamp = row.get("timestamp")
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value)):
            raise ValueError(f"{name}: not a finite numeric collector value")
        if (isinstance(timestamp, bool) or not isinstance(timestamp, (int, float))
                or not math.isfinite(timestamp) or timestamp < 1e15):
            raise ValueError(f"{name}: expected Prometheus millisecond export timestamp")
        age = time.time() - timestamp / 1e9
        if not -5 <= age <= 30:
            raise ValueError(f"{name}: stale or clock-skewed sample (age {age:.1f}s)")
        if (name.startswith("cpu_") or name.endswith("_percent")) and not 0 <= value <= 100:
            raise ValueError(f"{name}: percent outside 0..100")
        result[name] = {"value": value, "timestamp_ns": timestamp, "host": tags["host"]}
    if len({row["host"] for row in result.values()}) != 1:
        raise ValueError("required series do not describe one collector host")
    if not 0 <= result["mem_used"]["value"] <= result["mem_total"]["value"]:
        raise ValueError("RAM byte values are inconsistent")
    if result["mem_total"]["value"] <= 0:
        raise ValueError("RAM total is not positive")
    return result


try:
    health = json.loads(get(base, "/health"))
    if health.get("status") != "healthy" or health.get("version") != "2026.1.0":
        raise ValueError("expected healthy service reporting source-selected version 2026.1.0")
    first = sample()
    print(json.dumps({"sample": 1, "scope": "cpu_ram_only", "metrics": first}), flush=True)
    time.sleep(3)
    second = sample()
    print(json.dumps({"sample": 2, "scope": "cpu_ram_only", "metrics": second}), flush=True)
    for name in names:
        if second[name]["host"] != first[name]["host"]:
            raise ValueError(f"{name}: collector host changed between samples")
        if second[name]["timestamp_ns"] <= first[name]["timestamp_ns"]:
            raise ValueError(f"{name}: collector timestamp did not advance")
except (urllib.error.URLError, TimeoutError, ValueError, TypeError, AttributeError,
        KeyError, OSError) as exc:
    print(f"FAIL: Metrics CPU/RAM collector contract: {exc}", file=sys.stderr)
    sys.exit(1)

print("PASS: Metrics Manager live CPU/RAM collector contract only (two fresh, advancing samples).")
print("NOT VERIFIED: image digest, CPU frequency, GPU, NPU, power, model inference, "
      "Panther Lake compatibility, or complete application smoke.")
PY

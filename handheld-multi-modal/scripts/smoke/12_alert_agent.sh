#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${ALERT_URL:-}" ]]; then
  echo "BLOCKED: set ALERT_URL (e.g. http://127.0.0.1:8010) for an operator-started" >&2
  echo "Alert Agent instance running in rule/action-dispatch mode (AGENT_MODE=false)." >&2
  echo "Scope: health, one real action dispatch, dedup skip, tool registry. Does NOT" >&2
  echo "prove backend.speak_alert delivery -- no approved backend MCP server exists." >&2
  exit 2
fi
python="${SMOKE_PYTHON:-python3}"
command -v "$python" >/dev/null || {
  echo "BLOCKED: missing $python; this smoke installs nothing." >&2
  exit 2
}

exec "$python" - <<'PY'
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

base = os.environ["ALERT_URL"].rstrip("/")
parsed = urllib.parse.urlsplit(base)
if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
    print("BLOCKED: ALERT_URL must be an HTTP(S) base without credentials.", file=sys.stderr)
    sys.exit(2)


def request(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                  headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("response exceeds 1 MiB limit")
        return json.loads(raw.decode("utf-8"))


try:
    health = request("GET", "/api/v1/health")
    if health.get("status") != "healthy":
        raise ValueError(f"expected healthy status, got {health}")
    if health.get("adk_enabled") is not False:
        raise ValueError("expected adk_enabled=false (rule/action-dispatch mode required); "
                          "ADK/LLM in the alert-critical path is not permitted by this application")

    tools = request("GET", "/api/v1/tools")
    names = {t["name"] for t in tools.get("tools", [])}
    if "log_alert" not in names:
        raise ValueError(f"expected built-in log_alert tool registered, found {names}")

    source_id = f"smoke-{int(time.time())}"
    first = request("POST", "/api/v1/actions/execute",
                     {"data": {"source_id": source_id, "alert_name": "LOITERING",
                               "reason": "smoke test dispatch"}})
    if "log_alert" not in first.get("actions_taken", []):
        raise ValueError(f"expected log_alert dispatched on first alert, got {first}")

    second = request("POST", "/api/v1/actions/execute",
                      {"data": {"source_id": source_id, "alert_name": "LOITERING",
                                "reason": "smoke test dispatch repeat"}})
    if second.get("actions_taken"):
        raise ValueError(f"expected deduplicated (empty) actions_taken on immediate repeat, got {second}")

    print(json.dumps({"health": health, "tools": sorted(names),
                       "dispatch_1": first, "dispatch_2_deduped": second}, indent=2))
except (urllib.error.URLError, TimeoutError, ValueError, TypeError, KeyError, OSError) as exc:
    print(f"FAIL: Alert Agent rule-dispatch contract: {exc}", file=sys.stderr)
    sys.exit(1)

print("PASS: Alert Agent rule-based dispatch (ADK/MCP off), one real log_alert action,",
      "and one real field_hash dedup skip on repeat.")
print("NOT VERIFIED: backend.speak_alert delivery (no approved backend MCP server exists),",
      "trigger_webhook/capture_snapshot/publish_mqtt (require webhook target/image payload/",
      "MQTT subscriber not set up by this script -- see config/alert-agent/README.md",
      "for manual checks that exercise all four built-in tools), SSE/WebSocket",
      "fanout (manually verified separately, not by this script), image digest, Panther Lake.")
PY

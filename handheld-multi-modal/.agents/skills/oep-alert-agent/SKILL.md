---
name: oep-alert-agent
description: Use when changing how the handheld Backend dispatches alerts through the stock Open Edge Platform Alert Agent (rule-dispatch mode, field_hash dedup, SSE events).
---

# Alert Agent

Image `intel/alert-agent-service:2026.2.0`, source `edge-ai-libraries`
`microservices/alert-agent-service/` (release-2026.2.0). Compose service
`alert-agent`; host `127.0.0.1:8010` maps to container port `8000`.
Configuration is mounted read-only from `config/alert-agent/resources`
(`config.yaml`, `tools.json`, `mcp_servers.json`).

## How the application uses it

- Rule-dispatch mode only: `AGENT_MODE=false`, `MCP_ENABLED=false`. Do not
  enable ADK or an LLM, or start the optional `agent` Compose profile. Rule
  mode needs no GPU, NPU or model download.
- The Backend (`backend/app/alerts.py`) owns the alert rules. It posts
  matches to `POST /api/v1/actions/execute` with `field_hash` dedup on
  `metadata.track_id` and follows SSE `GET /api/v1/events`.
- Speech arbitration stays in the Backend (`backend/app/speech.py`), which
  calls Text To Speech directly. Do not move business rules or speech into
  the agent, and do not add a custom alert server.

## API notes

- `GET /api/v1/health` is liveness only.
- `/api/v1/actions/execute` takes the nested body from the service's
  `/openapi.json`, not the flat examples in the upstream README.
- `/api/v1/alerts` accepts flat JSON but reports `accepted` even when
  dispatch fails; use `actions/execute` when you need the result.
- SSE events: `init`, `alert_action`, `keepalive`, `error`. There is no
  replay or durable queue. Dedup hits do not emit events. WebSocket
  `/api/v1/ws` is also available.
- Unknown tools are skipped, tools run concurrently, and MCP `isError` is not
  checked. `actions_taken` does not prove that audio played.
- With MCP enabled, a server named `backend` advertising `speak_alert`
  registers as `mcp_backend_speak_alert`. Inspect `/api/v1/mcp/status` and
  `/api/v1/mcp/tools` instead of guessing names.

## Dedup

- `field_hash` is the only strategy. Hash real context fields such as
  `source_id` and `metadata.track_id`; `event_id` is not available.
- Keys are in memory, are set before execution and are lost on restart. A
  failed delivery can still suppress retries. Missing fields with
  `on_missing=skip` disable the check.
- The caller supplies `escalated`; rule mode does not count consecutive
  matches.

## Checks

```sh
curl -fsS http://127.0.0.1:8010/api/v1/health
ALERT_URL=http://127.0.0.1:8010 bash scripts/smoke/12_alert_agent.sh
```

The MQTT broker is `eclipse-mosquitto:2.0.22` from the application Compose
file; do not copy the floating `eclipse-mosquitto:2` tag from upstream.

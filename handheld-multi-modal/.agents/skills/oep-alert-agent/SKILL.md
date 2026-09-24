---
name: oep-alert-agent
description: Integrate the pinned stock OEP Alert Agent in non-LLM rule-dispatch mode; verify dedup, MCP speech-tool registration, and supported streams. Deployed and smoke-tested (rule-dispatch mode).
---

# OEP Alert Agent — repository override

Read `AGENTS.md` and `docs/application/service-contracts.md` first.
This is a locally authored integration skill, not an upstream official copy.
Source baseline `release-2026.2.0`, commit
`c2cdea0ef9b137986b14e6bbeebcb80dbde4c0e9`,
`microservices/alert-agent-service/`.
**Deployed and runtime-verified** on this Lunar Lake host
(`intel/alert-agent-service:2026.2.0-rc3`, host `127.0.0.1:8010`) — see
`STATUS.md` and `docs/application/evidence/alert-agent/smoke.log`,
not source-only.

## Required workflow

1. Pinned image `intel/alert-agent-service:2026.2.0-rc3` confirmed pulled and
   running.
2. `AGENT_MODE=false` confirmed via startup log and `/health`. Never use ADK,
   an LLM, or "fallback after LLM failure" in the critical path. Do not start
   the optional `agent` Compose profile. Rule mode needs no GPU/NPU device or
   model download.
3. Mounted `config/alert-agent/resources` (`config.yaml`, `tools.json`,
   `mcp_servers.json`) confirmed loaded. Stock config is demonstration
   policy, not this application's business rules.
4. `GET /api/v1/health` confirmed live; treat it only as liveness. The
   `/api/v1/actions/execute` body shape was confirmed directly from the
   deployed `/openapi.json` (nested body, not the flat published examples).
   `/api/v1/alerts` accepts flat JSON but masks dispatch errors with
   `accepted`.
5. For the future logical action `backend.speak_alert`, use a real backend MCP
   server advertising `speak_alert`. A server named `backend` produces the
   discovered name `mcp_backend_speak_alert`, not `backend.speak_alert` — this
   registration contract is confirmed/documented, but no backend exists yet
   to actually register, so `backend.speak_alert` delivery itself cannot be
   proven this session. `MCP_ENABLED=false` confirmed in this deployment.
   Set `MCP_ENABLED=true` only with valid server config; inspect
   `/api/v1/mcp/status` and `/api/v1/mcp/tools` for connection/input schema.
   Do not invent the backend URL or speech argument schema.
6. Confirmed this session: one real `log_alert` dispatch, one real dedup
   skip (`field_hash`), real `trigger_webhook`→local HTTP listener, real
   `capture_snapshot` PNG, real `publish_mqtt` on this repo's Mosquitto, real
   SSE `init`+`alert_action` fanout. Unknown tools are skipped; execution is
   concurrent; MCP `isError` is not checked by the wrapper. `actions_taken`
   alone does not prove playback. Do not implement final business rules,
   speech arbitration, or a custom server in the application.

## Dedup and streaming constraints

- `field_hash` is the only strategy. Select real context fields such as
  `source_id` and `metadata.track_id`; `event_id` is not available to this
  dedup context. Do not invent cooldown or Redis settings.
- TTL keys are in-memory, inserted before execution, non-durable, and not
  atomic across check/set. Failed delivery may still suppress retries.
  Missing fields with `on_missing=skip` disable that check.
- Caller supplies `escalated`; rule mode does not compute consecutive
  thresholds. Tools run concurrently despite upstream "ordered" descriptions.
- Stock SSE `/api/v1/events` and WS `/api/v1/ws` are supported. SSE events:
  `init`, `alert_action`, `keepalive`, `error`; no replay/durable queue.
  Dedup hits and negative answers do not emit action events.
- Do not copy floating `eclipse-mosquitto:2` from stock Compose into the pinned
  scaffold. Broker ownership belongs to the project's broker contract
  (`eclipse-mosquitto:2.0.22`, pinned in `compose/handheld-multi-modal.yaml`).

## Evidence discipline

Retain requests, responses, logs and actual side effects under the application
evidence (`docs/application/evidence/alert-agent/`) for any further work.
Report failures and missing interfaces; never relabel source validation as a
runtime pass. Keep the final Backend/UI contracts separate.

`scripts/smoke/12_alert_agent.sh` is **PASS** (`ALERT_URL=http://127.0.0.1:8010`),
not a blocked entry point — it confirms rule-based mode, one real `log_alert`
dispatch, and one real dedup skip end-to-end.

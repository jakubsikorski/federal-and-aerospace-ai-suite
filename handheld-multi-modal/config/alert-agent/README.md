# Alert Agent: PASS (rule-based dispatch, real on this host)

Pinned image `intel/alert-agent-service:2026.2.0-rc3`
(digest `sha256:4d9426486d00062e0805245b2d27e1107a014296eae65d38fa29318a5284880b`).
Deployed via `compose/bootstrap-compose.yaml` (`alert-agent` service) w/
`AGENT_MODE=false` (rule/action-dispatch only, no ADK/LLM in critical
path — confirmed by startup log `Starting Alert Agent Service | ADK=off
MCP=off`) and `MCP_ENABLED=false` (no approved backend MCP tool exists yet).
Reuses this repo's own pinned Mosquitto broker (`mqtt` service), not
stock Compose's floating `eclipse-mosquitto:2`.

`config/alert-agent/resources/{config.yaml,tools.json,mcp_servers.json}` are
unmodified copies of upstream example resources (edge-ai-libraries
`microservices/alert-agent-service/resources/`), except `mcp_servers.json`
has every server's `enabled` flipped to `false` (shipped `prometheus`
entry is placeholder URL, not real backend integration — see
`docs/bootstrap/service-contracts.md`).

## Run

```sh
docker compose -f compose/bootstrap-compose.yaml up -d alert-agent
ALERT_URL=http://127.0.0.1:8010 bash scripts/smoke/12_alert_agent.sh
```

## Evidence

Full session in `docs/bootstrap/evidence/alert-agent/smoke.log`.
`scripts/smoke/12_alert_agent.sh` PASS: health (`adk_enabled:false`),
`log_alert` registered, one real dispatch (`actions_taken:["log_alert"]`),
one real `field_hash` dedup skip on an immediate repeat
(`actions_taken:[]`, log line `Duplicate alert: key=dedup:LOITERING:...`).

Manually confirmed beyond script's scope (all real, not stubbed):

- **`trigger_webhook`**: CONCEALMENT alert → real HTTP POST reached local
  listener on `host.docker.internal:8999` w/ actual alert JSON body;
  container log `Webhook POST ... → HTTP 200`.
- **`capture_snapshot`**: CONCEALMENT alert w/ inline base64 PNG payload
  → real 68-byte valid PNG (`89 50 4e 47` magic bytes) written inside
  container at `/app/snapshots/<source_id>/<ALERT>_<timestamp>.png`; without
  image payload, tool silently absent from `actions_taken` (matches
  documented "only inline base64 images are saved" behavior — not error).
- **`publish_mqtt` / escalation**: three consecutive `escalated:true`
  CONCEALMENT alerts (dedup disabled per-request) → each dispatched
  `publish_mqtt` per subscription's `additional_tools`; `mosquitto_sub`
  on repo's own broker received real `alerts/concealment` messages w/
  actual alert JSON.
- **SSE fanout**: `GET /api/v1/events` connection received real `init`
  event followed by live `alert_action` event mirroring in-flight
  dispatch, fired from concurrent request.
- **`/api/v1/alerts`** (flexible ingestion): accepted minimal JSON body,
  returned `status:"accepted"`.
- **Manual tool invocation** (`POST /api/v1/tools/log_alert/invoke`):
  confirmed tool function requires `reason` as real positional
  argument — omitting it returns real
  `{"status":"error","result":{"error":"log_alert() missing 1 required
  positional argument: 'reason'"}}`; supplying it returns
  `status:"success"`. Genuine integration detail for whichever
  caller invokes tools directly (not through `/actions/execute`).
- **`/openapi.json`** confirmed exact `/api/v1/actions/execute` request
  body shape: `{"data": {...AlertActionRequest...}, "original_payload":
  {...}|null}` — nested-body FastAPI signature source contract
  predicted, not flat object.
- **`GET /api/v1/mcp/status`** → `{"enabled":false,"servers":[],
  "total_tools":0}`, confirming MCP actually off, not just configured off.

## `backend.speak_alert`: still not resolvable in this bootstrap

No backend service exists yet to advertise real `speak_alert` MCP tool, so
this can't be end-to-end proven here — remains explicit build-agent
work. Registration/invocation *contract*, however, is fully resolved and
documented in `docs/bootstrap/service-contracts.md` §"`backend.speak_alert`":
future backend advertises `speak_alert` over MCP (`transport:"http"`,
enabled `servers` entry named `backend`); discovery would register it as
`mcp_backend_speak_alert`; manual invocation would be
`POST /api/v1/mcp/tools/mcp_backend_speak_alert/invoke`. Don't implement
that backend tool here — explicit build-agent scope.

## Known limitations / gotchas

- Manual `/api/v1/tools/{name}/invoke` calls require every tool function's
  real positional arguments (e.g. `log_alert` needs `reason`); generic
  `{"parameters": {...}}` wrapper doesn't auto-supply missing arguments.
- `capture_snapshot` only saves inline base64 image payloads; `uri`-kind
  image payload doesn't trigger download (per source contract, not
  independently re-tested here).
- Dedup is process-local in-memory (TTL keyed by `field_hash`); container
  restart loses all keys — not durable exactly-once store.
- MQTT publish uses this repo's own pinned Mosquitto broker via
  `MQTT_BROKER=mqtt`, not stock Compose's floating-tag broker.
- No GPU/NPU device or `ovms-llm` profile started — not needed for
  rule-based dispatch; starting it would violate "no ADK/LLM in
  alert-critical path" requirement.

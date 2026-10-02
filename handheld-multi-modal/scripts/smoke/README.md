# Smoke commands

Run from repository root. See `STATUS.md` for current results. Exit 0 means scope
printed by a real test passed; functional failure is nonzero; explicit blocked
entry points exit 2 and are **not implemented smoke tests**.

| Entry point | Current scope |
|---|---|
| `bash scripts/smoke/01_mediamtx.sh` | **PASS.** Decodes one real RTSP frame from `/cam1`; verifies a real WHEP pull session against `/annotated` via in-container `whepclientsrc` (VP9), confirmed by MediaMTX's own "is reading from path 'annotated'" log line |
| `bash scripts/smoke/02_dlsps_cpu.sh` | **PASS.** Starts `config/dlsps/cpu/config.json` (must be the mounted config), confirms real ~23-30fps detection via `gvafpscounter` |
| `bash scripts/smoke/03_dlsps_gpu.sh` | **PASS.** Starts `config/dlsps/gpu/config.json` (must be the mounted config), confirms real ~29fps detection via `gvafpscounter` |
| `bash scripts/smoke/04_dlsps_npu.sh` | **PASS.** Starts `config/dlsps/npu/config.json` — the shipped default (must be the mounted config) — confirms real ~30fps detection, no NPU compile/plugin errors in logs |
| `bash scripts/smoke/05_mqtt.sh` | **PASS.** Starts the NPU pipeline, subscribes to `detections/live` for 10s, requires 5+ real messages (297 observed) via the documented `appsink`+`mqtt_publisher` mechanism |
| `bash scripts/smoke/06_snip.sh` | **PASS.** Starts the shipped crop-enabled NPU pipeline (`gvapython` callback wired into `config/dlsps/npu/config.json`, config passed via `crop-config`→`kwarg` pipeline parameter), asserts new matched `.jpg`+`.json` crop pairs appear in the mounted output volume (23+ observed in one run) |
| `bash scripts/smoke/07_chatqna_ingest.sh` | PASS against deployed stock container; uploads unique synthetic PDF/DOCX through `/documents`, saves fixture state |
| `bash scripts/smoke/08_chatqna_rag.sh` | PASS against deployed stock container; recovers random document-only identifiers and generates a grounded mission brief |
| `bash scripts/smoke/09_chat_general.sh` | PASS against deployed stock container; asks stock `/chat` about RTSP with mission corpus intact; checks general-knowledge answer and unchanged document list. One transient empty-completion run was observed and correctly failed nonzero (see `COMPONENTS.md`) |
| `bash scripts/smoke/10_audio_analyzer.sh` | PASS; needs `AUDIO_ANALYZER_URL` + `AUDIO_ANALYZER_SAMPLE_WAV`; posts real speech WAV to `/v1/audio/transcriptions`, checks `/health` and non-empty text |
| `bash scripts/smoke/11_tts.sh` | PASS (SpeechT5, owner-approved); needs `TTS_URL`; checks `/health`, `device=${TTS_DEVICE:-CPU}` via `/v1/model-info` (D56), and validates real synthesis returns a non-trivial WAV |
| `bash scripts/smoke/12_alert_agent.sh` | **PASS**, executed against deployed `intel/alert-agent-service:2026.2.0` (`ALERT_URL=http://127.0.0.1:8010`). Confirms rule-based mode, one real `log_alert` dispatch, one real dedup skip. Webhook/snapshot/MQTT/SSE and `backend.speak_alert` verified/documented manually — see `config/alert-agent/README.md`, `docs/application/evidence/alert-agent/smoke.log` |
| `bash scripts/smoke/13_metrics.sh` | **PASS**, executed against deployed `intel/metrics-manager:2026.2.0` (2026-09-29; earlier runs on 2026.1.0). Script scope is CPU/RAM-only by design; real GPU/NPU/temperature data additionally confirmed manually (see `config/metrics/README.md`, `docs/application/evidence/metrics/smoke.log`) |
| `make test-phase5` | **PASS.** Runs strict route/plan, interval-overlap, read-only budget, aggregate, completeness and stable-paging tests, then a live typed ten-minute query through stock Chat Q&A. Records independent malformed-plan safe failures rather than repairing model output; evidence under `docs/application/evidence/phase5/` |

## DLSPS / MediaMTX / MQTT (video pipeline)

Requires `dlsps` service from `compose/handheld-multi-modal.yaml` running,
plus MediaMTX and MQTT:

```sh
docker compose -f compose/handheld-multi-modal.yaml up -d
```

`scripts/smoke/_dlsps_lib.sh` holds shared helpers (container/REST names,
starting/stopping pipelines, waiting for real `gvafpscounter` line).
Override defaults via env vars (`DLSPS_CONTAINER`, `DLSPS_REST`,
`MQTT_CONTAINER`, etc.) if your container/service
names differ from compose defaults.

**Important:** scripts 02/03/04 each assume matching
`config/dlsps/{cpu,gpu,npu}/config.json` is file currently mounted at
`/home/pipeline-server/config.json` in DLSPS container (pipeline name
requested via REST must match `name` field inside that file). The mount
source is parameterized in `compose/handheld-multi-modal.yaml` via
`DLSPS_CONFIG_VARIANT` (default `npu`); copying a file into an unmounted
`/tmp` path and restarting does **not** change it — a restart re-reads the
same bind-mounted source, it doesn't swap it. Recreate the container with
the desired variant instead:

```sh
DLSPS_CONFIG_VARIANT=gpu docker compose -f compose/handheld-multi-modal.yaml up -d --force-recreate dlsps
bash scripts/smoke/03_dlsps_gpu.sh
# restore shipped default afterward:
docker compose -f compose/handheld-multi-modal.yaml up -d --force-recreate dlsps
```

Script 05 assumes NPU (shipped default) config is mounted. Script 06 is
intentional `BLOCKED` entry point (exit 2) — doesn't start a pipeline.

**Known codec gotcha for WHEP (01_mediamtx.sh):** this application's webrtc
destination is fed from JPEG-encoded appsink branch. DLSPS's WebRTC manager
(`src/server/webrtc/gstreamer_webrtc_manager.py`) always picks software
`vp9enc` encoder when source frame is JPEG/system-memory, regardless of
CPU/GPU/NPU detection device. `aiortc` (used by `scripts/smoke/whep_client.py`)
has no VP9 decoder, so can't be used to verify this particular pipeline
shape — 01_mediamtx.sh instead uses in-container `whepclientsrc`
GStreamer element (`rswebrtc` plugin), which supports VP9, and cross-checks
MediaMTX's own log for real "is reading from path 'annotated'" line.
`whep_client.py`/`aiortc` remains available as H264-only fallback for any
future pipeline variant feeding webrtc directly from VA-memory (no JPEG
re-encode), where DLSPS would instead pick `vah264enc`.

## MediaMTX transport

Scaffold Compose file starts only MediaMTX and Mosquitto. Doesn't start
DLSPS, publish test video, download models, or run any final application:

```sh
docker compose -f compose/handheld-multi-modal.yaml up -d mediamtx mqtt
```

Run the complete Phase 4 demo with one target:

```sh
make demo-up CAMERAS=2
```

This validates pinned local assets, creates TLS when absent, prepares the
re-encoded demo clips when needed, starts the Phase 4 service subset in
dependency order, launches `/cam1` and `/cam2`, waits for Backend MQTT feed
health plus `/annotated1` and `/annotated2`, and leaves the stack running.
If ignored assets live outside the checkout, pass their repository root
explicitly, for example `make demo-up CAMERAS=2 ASSET_ROOT=/path/to/assets`.
Use `make demo-status` to inspect it, `make demo-stop` to stop every service,
`make demo-up` again to restart, or `make demo-down` to remove containers.
See [device setup](../../docs/application/application-operations.md) for a new host.

The publisher definitions remain in `compose/cameras.yaml`. Prefer this target
over a hand-run `ffmpeg`: a manual publisher dies with its shell and silently
breaks every later video test. If you publish `classroom.avi` by hand, remux it
with `-fflags +genpts` first; its broken PTS cost ~132% extra CPU.

Then start the DLSPS pipeline pushing native WebRTC to `/annotatedN` (see
`config/dlsps/npu/README.md`, or just start the Backend, which owns pipeline
lifecycle from Phase 2 onward).
Media test must not be reported as DLSPS pass if synthetic publisher
used instead. Software generation of synthetic demo is not hardware
encode evidence.

Phase 5 typed-history smoke requires that full demo and is one target:

```sh
make test-phase5
```

The selected camera is authoritative. Stock Chat Q&A emits only a strict
route/plan; Backend owns read-only SQL. The smoke records up to five
independent generations because the stock model sometimes emits malformed
JSON, but it accepts no repaired/prefix JSON: malformed plans must return the
explicit safe failure and at least one request must pass the complete live
ten-minute contract.

Optional `aiortc`-based WHEP client (H264-only fallback, see gotcha above)
requires Python and pinned `aiortc`:

```sh
python3 -m venv .venv
.venv/bin/pip install -r scripts/smoke/requirements.txt
.venv/bin/python scripts/smoke/whep_client.py http://127.0.0.1:8889/annotated/whep
```

Performs SDP offer/answer, receives and decodes three video frames, and
deletes session. OPTIONS response or SDP exchange alone isn't a pass.

Stop only this project's infrastructure when finished:

```sh
docker compose -f compose/handheld-multi-modal.yaml down
```

Don't automatically rewrite `verified` fields from script exit codes. Review
scope, source stream provenance, device evidence and physical host identity.

## Chat Q&A

Start the backend from checked-in Compose (now declared as service `chatqna`,
previously a live-but-undeclared container):

```sh
docker compose -f compose/handheld-multi-modal.yaml up -d chatqna
```

Run 07, then 08, then 09 against configured headless service.
`CHATQNA_URL` defaults to `http://127.0.0.1:8888`;
`CHATQNA_EVIDENCE` defaults to `docs/application/evidence/chatqna/smoke`.
Tests never delete documents; uploads use unique fixture names.
General conversation is owner-approved retrieval-enabled `/chat` path,
not retrieval bypass. Successful API test alone doesn't identify actual
LLM execution device; retain deployment/runtime evidence too.

# Smoke commands

Run from repository root. Exit 0 means scope
printed by a real test passed; functional failure is nonzero; explicit blocked
entry points exit 2 and are **not implemented smoke tests**.

| Entry point | What it checks |
|---|---|
| `bash scripts/smoke/01_mediamtx.sh` | Decodes one real RTSP frame from `/cam1`; verifies a real WHEP pull session against a per-run `/annotated9NNNN` path via in-container `whepclientsrc` (H.264 for NPU/GPU; `SMOKE_WHEP_CODEC=VP9` for CPU), confirmed by MediaMTX's own "is reading from path" log line |
| `bash scripts/smoke/02_dlsps_cpu.sh` | Starts `config/dlsps/cpu/config.json` (must be the mounted config), confirms real detection throughput via `gvafpscounter` |
| `bash scripts/smoke/03_dlsps_gpu.sh` | Starts `config/dlsps/gpu/config.json` (must be the mounted config), confirms real detection throughput via `gvafpscounter` |
| `bash scripts/smoke/04_dlsps_npu.sh` | Starts `config/dlsps/npu/config.json` — the shipped default (must be the mounted config) — confirms real detection throughput, no NPU compile/plugin errors in logs |
| `bash scripts/smoke/05_mqtt.sh` | Starts the NPU pipeline, subscribes to `detections/live` for 10s, requires 5+ real messages via the documented `appsink`+`mqtt_publisher` mechanism |
| `bash scripts/smoke/06_snip.sh` | Starts the shipped crop-enabled NPU pipeline (`gvapython` callback wired into `config/dlsps/npu/config.json`, config passed via `crop-config`→`kwarg` pipeline parameter), asserts new matched `.jpg`+`.json` crop pairs appear in the mounted output volume |
| `bash scripts/smoke/07_chatqna_ingest.sh` | Against the deployed stock container; uploads unique synthetic PDF/DOCX through `/documents`, saves fixture state |
| `bash scripts/smoke/08_chatqna_rag.sh` | Against the deployed stock container; recovers random document-only identifiers and generates a grounded mission brief |
| `bash scripts/smoke/09_chat_general.sh` | Against the deployed stock container; asks stock `/chat` about RTSP with mission corpus intact; checks general-knowledge answer and unchanged document list. An empty completion fails nonzero |
| `bash scripts/smoke/10_audio_analyzer.sh` | Needs `AUDIO_ANALYZER_URL` + `AUDIO_ANALYZER_SAMPLE_WAV`; posts real speech WAV to `/v1/audio/transcriptions`, checks `/health` and non-empty text |
| `bash scripts/smoke/11_tts.sh` | SpeechT5; needs `TTS_URL`; checks `/health`, `device=${TTS_DEVICE:-CPU}` via `/v1/model-info`, and validates real synthesis returns a non-trivial WAV |
| `bash scripts/smoke/12_alert_agent.sh` | Against deployed `intel/alert-agent-service:2026.2.0` (`ALERT_URL=http://127.0.0.1:8010`). Confirms rule-based mode, one real `log_alert` dispatch, one real dedup skip. Webhook/snapshot/MQTT/SSE and `backend.speak_alert` are checked manually |
| `bash scripts/smoke/13_metrics.sh` | Against deployed `intel/metrics-manager:2026.2.0`. Script scope is CPU/RAM-only by design; GPU/NPU/temperature data are checked manually |
| `make test-history` | Runs strict route/plan, interval-overlap, read-only budget, aggregate, completeness and stable-paging tests, then a live typed ten-minute query through stock Chat Q&A. Records independent malformed-plan safe failures rather than repairing model output; output is written under `docs/application/evidence/history/` (local, gitignored) |

## DLSPS / MediaMTX / MQTT (video pipeline)

Requires `dlsps` service from `compose/handheld-multi-modal.yaml` running,
plus MediaMTX and MQTT:

```sh
make demo-up                                             # cam1 publisher + services
docker compose -f compose/handheld-multi-modal.yaml stop backend
bash scripts/smoke/04_dlsps_npu.sh                        # 01..06
make demo-up                                             # restore the application
```

The Backend owns every DLSPS instance and deletes any it does not own, so
scripts 01–06 exit 2 (BLOCKED) while it runs. Each smoke instance reads the
live `rtsp://mediamtx:8554/cam1` feed (`DLSPS_DEMO_URI`), sends a unique
`model_instance`, and tags crops/observations with stream id `smoke`
(`SMOKE_STREAM_ID`), which the Backend ignores as an unregistered camera, so
live camera history is never touched. Script 06 counts crops in
`data/dlsps/crops/smoke` through the DLSPS container (store owner UID 1999).

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

Scripts 01, 05 and 06 assume the NPU (shipped default) config is mounted.

**WHEP codec (01_mediamtx.sh):** the shipped NPU/GPU variants push VA-memory
frames that DLSPS encodes as H.264 in hardware (`vah264enc`); the CPU variant
negotiates VP9. The script offers only `SMOKE_WHEP_CODEC` (default `H264`), so
a mismatch fails with "codecs not supported by client" rather than passing on
a different leg. `whepclientsrc` (`rswebrtc`) decodes both;
`whep_client.py`/`aiortc` handles H.264 only.

## MediaMTX transport

To start only MediaMTX and Mosquitto (no DLSPS, test video, models or
application):

```sh
docker compose -f compose/handheld-multi-modal.yaml up -d mediamtx mqtt
```

Run the complete demo with one target:

```sh
make demo-up CAMERAS=2
```

This validates pinned local assets, creates TLS when absent, prepares the
re-encoded demo clips when needed, starts every service in
dependency order, launches `/cam1` and `/cam2`, waits for Backend MQTT feed
health plus `/annotated1` and `/annotated2`, and leaves the stack running.
If ignored assets live outside the checkout, pass their repository root
explicitly, for example `make demo-up CAMERAS=2 ASSET_ROOT=/path/to/assets`.
Use `make demo-status` to inspect it, `make demo-stop` to stop every service,
`make demo-up` again to restart, or `make demo-down` to remove containers.
See [device setup](../../docs/user-guide/application-operations.md) for a new host.

The publisher definitions remain in `compose/cameras.yaml`. Prefer this target
over a hand-run `ffmpeg`: a manual publisher dies with its shell and silently
breaks every later video test. If you publish `classroom.avi` by hand, remux it
with `-fflags +genpts` first; its broken PTS cost ~132% extra CPU.

Then start the DLSPS pipeline pushing native WebRTC to `/annotatedN` (or start the
Backend, which owns the pipeline lifecycle).
Media test must not be reported as DLSPS pass if synthetic publisher
used instead. Software generation of synthetic demo is not hardware
encode evidence.

The typed-history smoke requires that full demo and is one target:

```sh
make test-history
```

The selected camera is authoritative. Stock Chat Q&A emits only a strict
route/plan; Backend owns read-only SQL. Each live question must pass on its
first submission (the smoke never retries into a pass) and no repaired/prefix
JSON is accepted: malformed plans must return the explicit safe failure. The
target runs inside the `handheld/backend:dev` image (which provides `pypdf`).

Optional `aiortc`-based WHEP client (H264-only fallback, see gotcha above)
requires Python and pinned `aiortc`:

```sh
python3 -m venv .venv
.venv/bin/pip install -r scripts/smoke/requirements.txt
.venv/bin/python scripts/smoke/whep_client.py --cafile config/tls/server.crt \
  https://127.0.0.1:8443/annotated1/whep
```

Uses the proxy's WHEP route because MediaMTX signaling (8889) has no host
port in the localhost-only profile. Performs SDP offer/answer, receives and
decodes three video frames over loopback ICE, and deletes session. Add
`--relay-only` to use only the TURN server advertised by WHEP `OPTIONS`
(loopback Coturn, TURN/TCP) — the SSH-tunnel path. OPTIONS response or SDP exchange alone isn't a pass.

Stop only this project's infrastructure when finished:

```sh
docker compose -f compose/handheld-multi-modal.yaml down
```

Don't treat a script exit code alone as verification. Review
scope, source stream provenance, device evidence and physical host identity.

## Chat Q&A

Start the backend from checked-in Compose (service `chatqna`):

```sh
docker compose -f compose/handheld-multi-modal.yaml up -d chatqna
```

Run 07, then 08, then 09 against configured headless service.
`CHATQNA_URL` defaults to `http://127.0.0.1:8888`;
`CHATQNA_EVIDENCE` defaults to `docs/application/evidence/chatqna/smoke` (local, gitignored).
Tests never delete documents; uploads use unique fixture names.
General conversation uses the retrieval-enabled `/chat` path,
not retrieval bypass. Successful API test alone doesn't identify actual
LLM execution device; retain deployment/runtime evidence too.

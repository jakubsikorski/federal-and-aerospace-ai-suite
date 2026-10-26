# Application Operations

Run the commands from the `handheld-multi-modal` directory. The Makefile owns setup and the
application lifecycle; the Backend starts and stops the DL Streamer pipelines through the
Pipeline Server REST API, so no manual pipeline launcher is needed. Run `make` on its own for
the command list.

## Lifecycle Commands

```bash
# Deploy with your own cameras
make prereq             # check host tools and devices; download YOLO26s INT8
make up                 # start everything (runs prereq), no cameras: add them in the console
# Deploy with the demo camera feeds
make demo-prereq        # prereq + download and prepare the demo videos
make demo-up            # start everything plus looping demo cameras
make demo-mission       # optional: load the example mission document

make status             # services and per-camera feed health (alias demo-status)
make stop               # stop every service, including DLSPS (alias demo-stop)
make demo-up            # run again to restart (make stop first for a full restart)
make pipelines-restart  # Backend + DLSPS only; language models stay loaded
make speech-device TTS_DEVICE=GPU  # move SpeechT5 to GPU; TTS_DEVICE=CPU restores the default
make down               # remove containers; keep models, history, TLS (alias demo-down)

make clean              # down + remove generated per-device configuration
make clean-demo         # remove demo videos and publisher containers
make clean-history CONFIRM=yes  # tracks, evidence images, rules, alerts, mission documents
make clean-all CONFIRM=yes      # everything above + models, caches, TLS keys
```

`clean-history` and `clean-all` refuse to run without `CONFIRM=yes`. Container images are never
removed.

| Option | Default | Meaning |
|---|---|---|
| `CAMERAS` | `2` | Demo cameras for `demo-up`: `1` or `2`; add more in the console |
| `TTS_DEVICE` | `CPU` | SpeechT5 device, `CPU` or `GPU` |
| `ASSET_ROOT` | application directory | Root for `data/` and generated `config/` |
| `WAIT_SECONDS` | `360` | Video and telemetry readiness budget, in seconds |
| `MODEL_WAIT_SECONDS` | `3600` | YOLO download and export budget, in seconds |
| `AI_WAIT_SECONDS` | `3600` | Chat Q&A, Whisper, and SpeechT5 readiness budget, in seconds |

Pass the same overrides to later Make commands, for example
`make demo-up ASSET_ROOT=/path/to/assets CAMERAS=1`.

- `make up` starts with the demo cameras off and hidden. Add cameras in the console under
  **Cameras**. Console-added cameras persist across restarts.
- `make demo-up` also enables the demo cameras, starts their publishers, and waits for fresh
  detections and annotated video.
- Both drain existing pipelines first. A failed startup stops the application and exits with a
  non-zero code; it never substitutes another model or device.
- `make speech-device` restarts without demo feeds. In demo mode, use
  `make demo-up TTS_DEVICE=GPU` instead.

## First Start and Offline Starts

On the first start, Make starts the stock Model Download microservice, exports YOLO26s INT8
OpenVINO™ IR, and verifies it against the approved digests in
`config/dlsps/detector-profile.json`. `make demo-prereq` also downloads checksum-pinned demo
videos: the intel-iot-devkit `person-bicycle-car-detection.mp4` sample (CC BY 4.0) and Pexels
video 34622113 (Pexels License). Chat Q&A, Audio Analyzer, and Text To Speech download and
convert their own models through their unmodified stock services.

Later starts need no Internet access: caches are verified and loaded, and Chat Q&A starts in
offline mode once all its models are cached and exported. If `backend/` changed but the Backend
image rebuild cannot reach the registry, Make starts the previously built image and prints a
warning. A changed `backend/requirements.txt` or a model change in `config/` needs Internet
access again.

`config/devices.yaml`, `config/mediamtx.yml`, and `config/tls/` are generated per device under
`ASSET_ROOT`. Tracked configuration is not modified.

## Readiness Check

`make check` is read-only and downloads nothing. Each failing item names the command that fixes
it, and the target exits with a non-zero code until all items are ready.

- **Images** — every Compose image is present and the Backend image matches `backend/`.
- **Models and demo videos** — YOLO26s INT8 matches an approved hash, demo videos match their
  checksums, and every Chat Q&A, Whisper, and SpeechT5 OpenVINO model is complete.
- **Application** (only once started) — every container runs; each enabled camera has fresh
  detections and live annotated video; telemetry arrives; the LLM answers a short question;
  SpeechT5 synthesizes a sentence and Whisper transcribes it.

Items marked `--` are needed only by `make demo-up` and never fail the check.

## Copy to Another Device (Offline)

Prepare one device with Internet access (`make demo-up` once), then copy the application to
any number of devices without Internet access:

```bash
# On the prepared device (about 63 GB)
make bundle BUNDLE=/media/usb/handheld

# On the other device: Docker, Make, Python 3, curl, and OpenSSL installed
tar -xf /media/usb/handheld/source.tar
cd handheld-multi-modal
make bundle-load BUNDLE=/media/usb/handheld
make check                                     # every item should be ok
make demo-up                                   # no Internet access needed
```

| File | Contents |
|---|---|
| `images.tar` | `docker save` of every image in `images.txt`, including the built Backend |
| `images.txt` | The tagged images Compose uses |
| `source.tar` | The application source (committed files when run from a Git checkout) |
| `assets.tar` | Chat Q&A models, YOLO26s INT8, and demo videos |
| `volumes.tar` | Whisper and SpeechT5 models and caches (Docker volumes) |
| `SHA256SUMS` | Checked by `bundle-load` before anything is restored |

`IMAGES=no` skips `images.tar` when images are copied separately. History, mission documents,
alert rules, TLS keys, and detected device settings are not copied; each device creates its own
on the first start.

## Changing Models and Pipelines

| Change | Edit |
|---|---|
| Detector device (CPU, GPU, or NPU) | `DLSPS_CONFIG_VARIANT` (default `npu`) |
| Chat prompt template | `config/chatqna/config.yaml` |
| Evidence priority classes | `evidence_priority_classes` in `config/backend/backend.yaml` |
| Concepts the detector cannot report | `unsupported_concept_terms` in `config/backend/backend.yaml` |
| Camera location and coverage | Per-camera `description` in `config/backend/backend.yaml` |
| Mission upload limits | `mission_max_upload_mb`, `mission_max_pdf_pages`, `mission_max_docx_uncompressed_mb` in `config/backend/backend.yaml` |

To swap the detector model:

1. Update the download request in `config/model-download/yolo26s.json`.
2. Update `config/dlsps/detector-profile.json`: `pipeline_model`, the approved XML/BIN digests
   in `artifact_sets`, and the ordered `classes`.
3. Update the model path in each `config/dlsps/*/config.json`.
4. Record the model source and license in `third-party-components.spdx`.
5. Restart. The Backend refuses to start if the pipeline model path, IR digests, or exported
   class metadata disagree with the profile.

## Troubleshooting

- **Camera shows Starting, No frames received, or Signal lost** — check that the publisher or
  network camera sends H.264 RTSP to the address shown in **Cameras**, then run `make status`.
- **Annotated video stops or a pipeline is stuck** — run `make pipelines-restart`. It restarts
  the Backend and DLSPS together without reloading the language models. Do not restart the
  Backend container alone with `docker restart`.
- **Video is unplayable** — the annotated video is H.264 over WebRTC; the browser must support
  H.264. For remote access, see [Remote Access Through an SSH Tunnel](access-application.md#remote-access-through-an-ssh-tunnel).
- **A model card shows Starting** — the service has not answered yet; on the first start it may
  still be downloading or converting its model (up to an hour).
- **Startup fails behind a proxy** — include `localhost,127.0.0.1` in `no_proxy` and `NO_PROXY`;
  see [Configuring the Proxy](deploy-applications.md#optional-configuring-the-proxy).

## Tests

```bash
make test           # Backend checks in the Backend image against this directory
make test-ui        # browser checks; requires Firefox and geckodriver
make test-assets    # asset preparation and Make dry-run checks
make test-fuzz      # REST API fuzzing of the Backend (see backend/FUZZING.md)
make test-history   # history query checks against the running demo (needs demo-up)
```

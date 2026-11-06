<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Manage Pipelines

How to **start, stop, and check** the DL Streamer Pipeline Server pipelines
in this application. There are two ways to do it, from most to least
automated:

| Method | When to use | Driven by |
| --- | --- | --- |
| [Pipeline manager](#pipeline-manager-make-start-rtsp) (`make start-rtsp`) | Normal operation: starts/stops every configured camera as the vehicle arms/disarms | `configs/cameras-pymavlink.yaml` (or `cameras-uavsdk.yaml`) |
| [REST API](#rest-api) | Scripting, or a one-off pipeline with custom parameters | `curl` to `https://<HOST_IP>/pipelines/...` |

To change what a pipeline *does* (its GStreamer elements or parameter
schema), see [Modify Pipelines](./modify-pipelines.md).

## Pipeline manager (`make start-rtsp`)

`scripts/mavlink_pipeline_manager.py` runs inside `dlstreamer-pipeline-server`
and reacts to the vehicle's armed state:

- **ARMED** → re-reads the camera file and starts one pipeline per enabled
  camera, one after another.
- **DISARMED** → stops the pipelines it started.

The armed state comes from MAVLink (standalone mode) or from the UAV Mission
Compute SDK's MQTT topic `uav/<UAV_ID>/telemetry/status` (`ARM_SOURCE=mqtt`,
set by `docker-compose-uavsdk.yml`). In SDK mode a camera whose RTSP source
is not yet readable is skipped and retried without blocking the others.

### Start

```bash
make check-cameras   # validate the camera file and show which pipelines would run
make start-rtsp      # wait for the vehicle; start on ARM, stop on DISARM
```

`CAMERAS=<file>` points both targets at another camera file. The manager
runs in the foreground; Ctrl+C exits it but leaves already-running pipelines
up.

### Status

```bash
make cameras-status
```

Lists each camera, the pipeline the file asks for, the pipeline actually
running, its state and FPS, and whether they match (`NO: restart to apply
the file` after an edit made while armed).

### Stop

Disarm the vehicle. To stop everything the manager started while it keeps
waiting for the vehicle, use the REST API
[stop-all](#stop-all-running-pipelines) below.

### Edit the camera file

The file is re-read at every arming. An edit while disarmed applies at the
next arm; an edit while armed is logged and applies at the *next* arm. A file
with errors is refused and the previous configuration keeps running — see
[Configure the Cameras](../camera-management/configure-cameras.md).

## REST API

The API is reached through nginx at `https://<HOST_IP>/` (self-signed
certificate, use `curl -k`); port `8081` is not published to the host.

### List the available pipelines

Pipelines are defined in `configs/config-pymavlink.json` (standalone) or
`configs/config-uavsdk.json` (SDK mode), mounted as
`/home/pipeline-server/config.json`:

```bash
docker exec uav-va-dlstreamer-pipeline-server python3 -c \
  "import json; print('\n'.join(p['name'] for p in json.load(open('/home/pipeline-server/config.json'))['config']['pipelines']))"
```

Both modes use the same names — `uav_object_detection_<det>` (detection
only) and `uav_object_detection_<det>_<vlm>` (detection + captions), with
`<det>` = `cpu`/`gpu`/`npu` and `<vlm>` = `cpu`/`gpu`; standalone mode adds
`uav_camera_npu` and `uav_camera_npu_gpu` for live cameras. See
[Modify Pipelines — naming](./modify-pipelines.md#pipeline-naming).

### Start a pipeline

`POST /pipelines/user_defined_pipelines/<name>`. The body sets the RTSP
output path and any of the pipeline's `parameters`; everything omitted
takes the pipeline's defaults. The response is the **instance ID**.

```bash
INSTANCE_ID=$(curl -k -s -X POST \
  https://<HOST_IP>/pipelines/user_defined_pipelines/uav_object_detection_gpu \
  -H 'Content-Type: application/json' \
  -d '{
    "destination": {
      "frame": {"type": "rtsp", "path": "uav-my-camera", "encoding": "h264"}
    },
    "parameters": {
      "video": "uav_sample_bottom.avi",
      "overlay_label": "my-camera-GPU"
    }
  }' | tr -d '"')
echo "Instance ID: $INSTANCE_ID"
```

Useful request fields:

| Field | Purpose |
| --- | --- |
| `destination.frame.path` | Stream name: `rtsp://<HOST_IP>:8555/<path>`. Keep `"encoding": "h264"` (the ground station's relay needs H.264). |
| `destination.frame.overlay: false` | Hide the detection boxes and telemetry text. |
| `destination.metadata` | Add `{"type": "file", "path": "/tmp/results.jsonl", "format": "json-lines"}` to keep a per-frame detection log (see [View Detection Logs](../../how-to-guides/view-detection-logs.md)). |
| `parameters.video` | Standalone mode: file name in `resources/videos`. |
| `parameters.rtsp_url` | SDK mode: the camera's RTSP source. |
| `parameters.detection_model` | Container path of the OpenVINO™ IR model (see [Modify the Detection Model](../model-and-video-management/modify-model.md)). |
| `parameters.captioner_model_path`, `captioner_prompt`, `captioner_max_new_tokens`, `mqtt_topic` | Captioning pipelines only (see [Change the Captioning Model](../model-and-video-management/vlm-model.md)). |
| `parameters.captioner_instance_id` | Captioning pipelines only. Streams with the same id share one loaded VLM (one copy in memory, requests served in turn). The camera manager sets it from the VLM device, model, and `max_new_tokens`, because the token limit belongs to the shared instance (the first stream's value wins); the prompt stays per stream. Use a different id only to give a stream its own VLM copy. |

The full schema per pipeline is in the config file; see
[Modify Pipelines — the parameters schema](./modify-pipelines.md#the-parameters-schema).

### Check status

One instance:

```bash
curl -k -s https://<HOST_IP>/pipelines/${INSTANCE_ID}/status | python3 -m json.tool
```

Every instance (state, `avg_fps`, `id`):

```bash
curl -k -s https://<HOST_IP>/pipelines/status | python3 -m json.tool
```

`"state": "RUNNING"` is healthy. `QUEUED` that never changes means the
pipeline is waiting on a resource held by another instance (same
`detection_instance_id`, same `/dev/video*` device). `ERROR` — check
`docker logs uav-va-dlstreamer-pipeline-server`. A GPU/NPU pipeline that silently
fell back to CPU still reports `RUNNING`, just at CPU-typical `avg_fps`.

### Stop a pipeline

```bash
curl -k -X DELETE https://<HOST_IP>/pipelines/${INSTANCE_ID}
```

The RTSP stream path stays registered for a few seconds after the instance
reports stopped; starting a new pipeline on the same path right away fails
with `RTSP Stream ... already exists` — wait briefly and retry (the manager
does this automatically).

### Stop all running pipelines

```bash
for id in $(curl -k -s https://<HOST_IP>/pipelines/status | python3 -c \
  "import json,sys; print(' '.join(p['id'] for p in json.load(sys.stdin) if p.get('state')=='RUNNING'))"); do
  curl -k -s -X DELETE https://<HOST_IP>/pipelines/$id
done
```

This also stops pipelines started by the manager. `make pymav-down` / `make uavsdk-down` stops the whole stack instead.

## Troubleshooting

Common symptoms — `404`/`400` on POST, `model-instance-id ... errored out`,
`RTSP Stream ... already exists`, a pipeline stuck in `QUEUED`, GPU running
at CPU speed — are covered in
[Troubleshooting — Pipelines](../../how-to-guides/troubleshooting.md#pipelines).
A camera that does not start on arm is explained by `make check-cameras`
(see [Configure the Cameras — When something is wrong](../camera-management/configure-cameras.md#when-something-is-wrong)).

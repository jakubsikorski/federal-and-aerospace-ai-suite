<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Modify Pipelines

A pipeline is a GStreamer launch string plus a JSON Schema that says which
parts of it may be overridden per start request. They are defined in
`configs/config-pymavlink.json` (standalone mode) and
`configs/config-uavsdk.json` (UAV Mission Compute SDK mode), each mounted
into the container as `/home/pipeline-server/config.json`. This page shows
how to change an element's properties, expose a new parameter, or add a
pipeline. To start/stop/check pipelines, see
[Manage Pipelines](./manage-pipelines.md).

## Anatomy of a pipeline entry

```bash
{
  "name": "uav_object_detection_cpu",
  "source": "gstreamer",
  "queue_maxsize": 50,
  "pipeline": "multifilesrc location=/home/pipeline-server/resources/videos/{parameters[video]} loop=true ! h264parse ! decodebin3 ! ... ! gvadetect device=CPU model={parameters[detection_model]} threshold=0.4 ... ! appsink name=appsink",
  "parameters": {
    "type": "object",
    "properties": {
      "video": {"type": "string", "pattern": "^[A-Za-z0-9_.-]+\\.(avi|mp4|mkv|ts)$", "default": "uav_sample_front.avi"},
      "detection_model": {"type": "string", "pattern": "^/home/pipeline-server/resources/models/[A-Za-z0-9_./-]+\\.xml$", "default": "/home/pipeline-server/resources/models/yolo11s/yolo11s_openvino_model/yolo11s.xml"}
    }
  },
  "auto_start": false
}
```

| Field | Meaning |
| --- | --- |
| `name` | Identifier used in the REST path and derived by the camera file from each camera's devices (see [naming](#pipeline-naming)). |
| `pipeline` | The GStreamer string. `{parameters[x]}` placeholders are filled from the start request, or from the schema `default`. |
| `parameters` | JSON Schema of the overridable values. Anything in the string that is **not** a placeholder is fixed and can only be changed by editing this file. |
| `auto_start` | Always `false` here — pipelines are started by the manager or the REST API, never when the container starts. |

## Pipeline naming

The camera file never names a pipeline; `scripts/camera_config.py` derives
it from each camera's source and devices:

| Source | Captioning | Pipeline | Config file |
| --- | --- | --- | --- |
| `video:` file or `rtsp:` stream | off | `uav_object_detection_<det>` | both |
| `video:` file or `rtsp:` stream | on | `uav_object_detection_<det>_<vlm>` | both |
| `device:` live camera | off | `uav_camera_<det>` | pymavlink (`npu` shipped; CPU/GPU in `config-pymavlink-realsense.json`) |
| `device:` live camera | on | `uav_camera_<det>_<vlm>` | same |

`<det>` is `cpu`/`gpu`/`npu`, `<vlm>` is `cpu`/`gpu`. The device is baked
into each string's `gvadetect device=` / `gvagenai device=`, which is why
there is one pipeline per combination. `config-uavsdk.json` holds the same
nine `uav_object_detection_*` names with an `rtspsrc` source and an
`rtsp_url` parameter instead of `video`.

## Change an element's property

Edit the `pipeline` string, then recreate the container so the file is
re-read (edits never apply to running pipelines):

```bash
make pymav-down && make pymav-up      # or make uavsdk-down && make uavsdk-up
make start-rtsp
```

Properties commonly worth tuning:

| Element | Property | Effect |
| --- | --- | --- |
| `gvadetect` | `threshold=0.4` | Confidence cutoff for keeping a detection. Lower it if a different model produces no boxes. |
| `gvadetect` | `inference-interval=1` | Run detection every Nth frame to trade latency for throughput. |
| `gvadetect` | `ie-config="CPU_THROUGHPUT_STREAMS=2"` (CPU pipelines) | Parallel inference streams on the CPU. |
| `videorate ! video/x-raw,framerate=1/5` (captioning branch) | frame rate | How often a frame is sent to the VLM — one every 5 s by default. Captions slower than this interval fall behind. |
| `gvagenai` | `device=` | Where the VLM runs; kept in step with the pipeline name. |

Validate a hand-edited string with `gst-launch-1.0` (placeholders replaced
by real values) before putting it in the file — a bad string only fails at
pipeline start, not at config load.

## Add or change a parameter

Add an entry to `parameters.properties` with a `type`, a `default`, and a
`pattern` or `minimum`/`maximum` that restricts what callers can set, then
reference it in the string as `{parameters[<name>]}`:

```bash
"frame_width": {"type": "integer", "minimum": 0, "default": 896}
```

Callers set it under `parameters` in the start request
([Manage Pipelines — Start a pipeline](./manage-pipelines.md#start-a-pipeline)).
To make the camera file set it too, extend `scripts/camera_config.py`
(`build_plan()` builds the request from the camera's fields).

## The parameters schema

The shipped pipelines expose:

| Pipeline family | Parameters |
| --- | --- |
| `uav_object_detection_<det>` (pymavlink) | `video`, `detection_model`, `detection_instance_id`, `overlay_label` |
| `uav_object_detection_<det>` (uavsdk) | `rtsp_url`, `detection_model`, `detection_instance_id`, `overlay_label` |
| `uav_object_detection_<det>_<vlm>` | the above plus `captioner_model_path`, `captioner_instance_id`, `captioner_prompt`, `captioner_max_new_tokens`, `frame_width`, `frame_height`, `mqtt_topic` |
| `uav_camera_<det>[_<vlm>]` | as the file-source pipelines, with `device`, `camera_width`, `camera_height`, `camera_fps` instead of `video` |

Each value is checked against its `pattern`; a request that does not match
is rejected with `400` (for example `detection_model` must be an `.xml` under
`/home/pipeline-server/resources/models/`).

## Add a pipeline

1. Copy the closest entry in the config file and give it a unique `name`.
2. Edit the string and schema. For a new device/VLM combination keep the
   naming convention above so the camera file can select it without any
   code change; a live-camera CPU/GPU pipeline can be copied verbatim from
   `configs/config-pymavlink-realsense.json`.
3. Recreate the container (`make pymav-down && make pymav-up`).
4. `make check-cameras` lists any camera whose derived pipeline name is not
   in the file. A pipeline with a name outside the convention is reachable
   only through the REST API.

## Troubleshooting

An edit that has no effect, a container that restarts after an edit,
`pipeline ... is not in the pipeline server's config.json` from
`make check-cameras`, and `gst_parse_error: no element ...` are covered in
[Troubleshooting — Pipelines](../../how-to-guides/troubleshooting.md#pipelines).

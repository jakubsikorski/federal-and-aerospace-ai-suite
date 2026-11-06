<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Configure the Cameras

`make start-rtsp` streams the cameras listed in `configs/cameras-pymavlink.yaml` when the vehicle is armed. For every camera you
choose its source (a video file or a live camera), which device runs object detection, whether a vision-language model (VLM) writes scene
captions (and, if so, its device, model, and prompt), and whether detections are drawn on the stream. Edit the
file, apply it, and check the result; no pipeline definition has to be touched.

## The file

```yaml
version: 1

cameras:
  - id: front_camera                 # a-z, 0-9 and _ ; also the stream name uav-front-camera and caption topic uav/caption/front_camera
    enabled: true                    # false skips the camera without deleting its settings
    video: uav_sample_front.avi      # file name in resources/videos (or device: for a live camera, see below)
    overlay: true                    # false hides the detection boxes AND the telemetry text
    detection:
      device: npu                    # cpu | gpu | npu
      model: yolo11s/yolo11s_openvino_model/yolo11s.xml   # path below resources/models
    captioning:
      enabled: true                  # false = detection only
      device: gpu                    # cpu | gpu  (the VLM does not run on the NPU)
      model: InternVL2-1B            # folder in resources/ov_models/<device>/
      prompt: Describe what you see in the scene.
      max_new_tokens: 50
```

The shipped file has three cameras, `front_camera`, `bottom_camera`, and `back_camera`, each with detection on the NPU and
captions from the VLM on the GPU. Every field except `id` is optional and falls back to the values above (captioning is off
unless `enabled: true`).

| Field | Meaning |
| --- | --- |
| `id` | Camera name: lowercase letters, digits, and `_`. Also names the RTSP stream (`uav-<id>` with `_` replaced by `-`) and the caption topic. |
| `enabled` | `false` leaves the camera out. |
| `video` / `device` / `rtsp` | The source: one of `video`, `device` for a live camera ([below](#live-cameras-realsense-usb-webcams)), or `rtsp` for an RTSP stream ([below](#rtsp-streams-uav-mission-compute-sdk)), never two. `video` is a file in `resources/videos` (`.avi`, `.mp4`, `.mkv`, or `.ts`, no folders). Prepare your own with [`scripts/convert_video.sh`](../model-and-video-management/use-your-own-video.md). |
| `overlay` | `false` removes the detection boxes and the telemetry text from the stream. |
| `detection.device` / `.model` | Where and with which model objects are detected. The model path is relative to `resources/models`. Cameras with the same device and model share one loaded model. |
| `captioning.enabled` | `true` adds captions. They are published on MQTT `uav/caption/<id>`. |
| `captioning.device` / `.model` / `.prompt` / `.max_new_tokens` | Where, with which model, with which prompt, and how long the captions are. The prompt may contain only letters, digits, spaces, and `, . ? ' -`. |
| `rtsp_path`, `mqtt_topic` | Optional overrides of the stream name and caption topic. |

### Which pipeline runs

The pipelines are chosen from the source and the devices. `configs/config-pymavlink.json` has the nine for
video files and two for live cameras (`uav_camera_npu`, `uav_camera_npu_gpu`); `configs/config-pymavlink-realsense.json`
holds all nine live camera pipelines for reference (copy the ones you need into the first file and recreate the container):

| Source | Captioning | Pipeline | Combinations |
| --- | --- | --- | --- |
| `video` (file) | off | `uav_object_detection_<det>` | `cpu`, `gpu`, `npu` |
| `video` (file) | on | `uav_object_detection_<det>_<vlm>` | `<det>` = `cpu`/`gpu`/`npu` with `<vlm>` = `cpu`/`gpu` |
| `device` (live camera) | off | `uav_camera_<det>` | `cpu`, `gpu`, `npu` |
| `device` (live camera) | on | `uav_camera_<det>_<vlm>` | as above |

A camera that needs a pipeline the server does not have is reported by `make check-cameras`.
A `uav_camera_*` pipeline is its video twin with a `v4l2src` source instead of a file source.
Each pipeline is tuned for its detection device, so the file chooses a pipeline and never edits one.

## Apply a change and see the effect

1. **Check the file without starting anything**

   ```bash
   make check-cameras
   ```

   prints every problem in the file, or what would run:

   ```text
   /home/pipeline-server/configs/cameras-pymavlink.yaml: valid, 3 camera(s) enabled
   camera         pipeline                      stream path        overlay  detection / captioning
   front_camera   uav_object_detection_npu_gpu  uav-front-camera   on       NPU yolo11s.xml + GPU InternVL2-1B
   ...
   ```

   Use `make check-cameras CAMERAS=/path/to/other.yaml` for another file.

2. **Apply it.** `make start-rtsp` reads the file when it starts **and again every time the vehicle is armed**, so
   after an edit either start `make start-rtsp` again or disarm and arm. The manager prints what it found:

   - an edit while disarmed is loaded immediately (`[config] cameras-pymavlink.yaml: 3 camera(s) enabled` and the table);
   - an edit that is not valid is refused and the previous configuration stays (`[config] ... has errors, keeping the
     previous configuration:` followed by the problems), so a typo never leaves the vehicle without video;
   - an edit while armed is noted (`[config] changed while armed: it applies the next time the vehicle is armed`);
     the cameras that are streaming keep running unchanged.

   Without a vehicle, `docker exec -it uav-va-dlstreamer-pipeline-server python3 /home/pipeline-server/scripts/pipeline_manager.py --start-now`
   starts the cameras right away and stops them on Ctrl+C.

3. **Compare the file with what is running**

   ```bash
   make cameras-status
   ```

   ```text
   camera         asked for (file)              running                       state    fps   matches
   front_camera   uav_object_detection_npu_gpu  uav_object_detection_npu_gpu  RUNNING  30.2  yes
   bottom_camera  uav_object_detection_gpu      uav_object_detection_gpu      RUNNING  30.0  NO: restart to apply the file
   ```

   `NO` means the file asks for something the running pipeline does not have, for example after an edit while armed.

4. **On the stream and in the dashboard.** The telemetry text at the top left of the stream names the camera and its
   devices (for example `front-camera-NPU-GPU-Caption`). In the [ground-station dashboard](https://github.com/open-edge-platform/federal-and-aerospace-ai-suite/blob/main/uav-ground-station/README.md)
   every camera has a card with its devices as badges (`NPU`, `VLM GPU`, `no overlay`);
   hover the card header for the models, prompt, and video. Streams are
   `rtsp://<HOST_IP>:8555/uav-<camera>`, captions `mosquitto_sub -h <HOST_IP> -t uav/caption/<camera>`.

## Live cameras (RealSense, USB webcams)

A live camera is a camera whose source is `device` instead of `video`. Everything else
(detection, captioning, overlay) is the same, and RealSense cameras and ordinary UVC webcams
are treated alike. They run the `uav_camera_*` pipelines.

The shipped `configs/cameras-pymavlink.yaml` has `realsense_camera` disabled; set `enabled: true`
once a camera is connected.

```yaml
  - id: realsense_front
    device: /dev/v4l/by-id/usb-Intel_R__RealSense_TM__Depth_Camera_435_Intel_R__RealSense_TM__Depth_Camera_435-video-index0
    width: 1280                      # optional, default 1280
    height: 720                      # optional, default 720
    fps: 30                          # optional, default 30
    detection: {device: npu}
```

1. **Find the node.** `v4l2-ctl --list-devices` lists the cameras. A RealSense exposes several nodes (depth, infrared,
  color): use the color one, the one whose `v4l2-ctl -d <node> --list-formats-ext` shows YUYV or UYVY. The `index` number in
  the `/dev/v4l/by-id/` name does not tell which one it is (in testing on a D435, the color node was `index0`).
2. **Prefer a stable name.** `/dev/videoN` numbers change with the plug order and after a reboot. The links in
   `/dev/v4l/by-id/` (they follow the camera) and `/dev/v4l/by-path/` (they follow the USB port) do not. `device` accepts
  `/dev/videoN`, `/dev/v4l/by-id/<name>`, and `/dev/v4l/by-path/<name>`.
3. **Make it visible to the container.** Put the same path in `.env` as `CAMERA_DEVICE_1` (up to `CAMERA_DEVICE_4`;
   `make init` fills `CAMERA_DEVICE_1` with the first USB camera it finds) and recreate the container with `make pymav-up`.
4. **Check.** `make check-cameras` reports a device that is missing. A camera that is not plugged in is left out with a
  warning (in `check-cameras`, `cameras-status`, and the manager log), and the other cameras still start.

> [!NOTE]
> Each device can be used by one camera only, and the images must be raw video (not MJPEG) at
  the requested size. Several cameras on one USB controller can exceed its bandwidth; spread
  them over different ports.

## RTSP streams (UAV Mission Compute SDK)

A camera can read an RTSP stream instead of a file or a device. The UAV Mission Compute SDK stack (`make uavsdk-up`) does
this: it mounts [`configs/cameras-uavsdk.yaml`](https://github.com/open-edge-platform/federal-and-aerospace-ai-suite/blob/main/uav-vision-analytics/configs/cameras-uavsdk.yaml) (`CAMERAS_CONFIG`) with the same format and the
same tools (`make check-cameras`, `make start-rtsp`, `make cameras-status`):

```yaml
  - id: nadir_camera
    rtsp: rtsp://host.docker.internal:8554/{uav_id}/nadir     # H.264 over RTSP, no user or password
    detection: {device: cpu}
```

- `{uav_id}` is replaced by `UAV_ID` from `.env` (default `uav-1`), so the file works for any vehicle ID.
- The pipelines have the same names as for video files (`uav_object_detection_<det>[_<vlm>]`); `config-uavsdk.json` defines them
  with an RTSP source, so the dashboard and the tools do not care where the images come from.
- The manager is armed by the SDK's MQTT status topic `uav/<UAV_ID>/telemetry/status` (`{"armed": true}`) instead of MAVLink,
  and starts a camera only when its stream can be read (`ffprobe`); a camera whose stream is missing is skipped.
- The shipped file has the nadir camera on the CPU, the forward camera on the GPU, and the rear camera on the NPU (skipped on a host
  without an NPU).

## Examples

Detection only on the GPU, with no overlay, and a video of your own:

```yaml
  - id: my_camera
    video: my_clip.avi
    overlay: false
    detection: {device: gpu}
```

Captions from the CPU VLM and a different question:

```yaml
  - id: front_camera
    detection: {device: npu}
    captioning:
      enabled: true
      device: cpu
      prompt: Count the cars and the buses.
```

## When something is wrong

`make check-cameras` explains every problem on its own line, for example:

```text
cameras.0.captioning.device: Input should be 'cpu' or 'gpu'
cameras.1.video: must be a file name like uav_sample_front.avi (avi, mp4, mkv or ts, no folders)
cameras[front_camera]: video not found: /home/pipeline-server/resources/videos/clip.avi
```

A misspelled key is an error too (`overlays: false` is reported, not ignored). If the manager is started with a file that
cannot be used it refuses to start and says so. Pipeline start failures are retried a few times, because the stream name of a
camera that was stopped a moment ago is still registered for a short while.

## Notes

- The streams are named after the cameras (`uav-front-camera`), no longer after the devices (`uav-mavlink-cpu`). Update
  QGroundControl, VLC or script URLs; the caption topics are `uav/caption/<camera>` as well.
- Captions need the VLM models: `make vlm-model` (see the [README](https://github.com/open-edge-platform/federal-and-aerospace-ai-suite/blob/main/uav-vision-analytics/README.md)).
- Detection on the CPU cannot keep up with a 30 fps video (about 17 to 25 fps); the NPU and the GPU can.
- The pipelines publish no per-frame detection log. To keep one when starting a pipeline by hand, add a
  `destination.metadata` file destination to the start request.

## For developers

`scripts/camera_config.py` has no dependency on MAVLink or on the manager, so other components can reuse it:
`parse_config(dict)` validates a configuration (a web UI or REST endpoint passes its JSON body to it and gets every
problem back), `build_plan(config)` returns the pipeline and the start request for each enabled camera, and `json_schema()`
describes the file for forms.

# How to Customize UAV Vision Analytics

These tutorials cover what can be changed in the application and how:
cameras and video sources, detection and captioning models, pipeline
definitions, what is drawn on the stream, and the ground station's live
search. For guides on *operating* the application as shipped (QGroundControl,
multi-UAV ground station, deployment modes, troubleshooting), see
[How-To Guides](./how-to-guides.md).

Every change below is applied the same way: edit the file, run
`make check-cameras` to validate, then `make start-rtsp` (or disarm/arm) to
restart the affected pipelines.

## Camera Management

- [Configure the Cameras](./how-to-customize/camera-management/configure-cameras.md) —
  `configs/cameras-pymavlink.yaml` / `cameras-uavsdk.yaml`: each camera's
  source, detection and captioning devices, models, prompt and overlay, and
  how a camera maps to a pipeline
- [Intel RealSense Camera](./how-to-customize/camera-management/realsense-guide.md) —
  Use a RealSense or USB webcam as a live camera source

## Model and Video Management

- [Manage the Detection Model](./how-to-customize/model-and-video-management/model.md) —
  The bundled YOLO11s model: `make model`, where the files live, how the
  pipelines find them, re-export
- [Modify the Detection Model](./how-to-customize/model-and-video-management/modify-model.md) —
  Export another Ultralytics size or your own OpenVINO IR detector and point
  cameras at it
- [Change the Captioning (VLM) Model](./how-to-customize/model-and-video-management/vlm-model.md) —
  The OpenVINO IR format `gvagenai` needs, downloading or exporting a
  different VLM, prompt and caption length
- [Change the Video Source](./how-to-customize/model-and-video-management/change-video-source.md) —
  Switch a camera between a video file, a live camera and an RTSP stream
- [Use Your Own Videos](./how-to-customize/model-and-video-management/use-your-own-video.md) —
  Convert footage with `scripts/convert_video.sh` and assign it to a camera

## Pipeline Management

- [Manage Pipelines](./how-to-customize/pipeline-management/manage-pipelines.md) —
  Start, stop and check pipelines: the arm/disarm manager, the per-device
  script, and the REST API (status, stop, stop all)
- [Modify Pipelines](./how-to-customize/pipeline-management/modify-pipelines.md) —
  The pipeline definition in `configs/config-*.json`: element properties,
  the `parameters` schema, naming, adding a pipeline

## Telemetry Overlay

- [Add, Modify, or Remove a Telemetry Overlay Field](./how-to-customize/telemetry-overlay/customize-telemetry-overlay.md) —
  Edit the `gvapython` overlay scripts for pymavlink / UAVSDK mode

## Search Tuning

- [Tune VSS Live Search](./how-to-customize/search-tuning/vss-search-tuning.md) —
  Result count, relevance filters, indexing rate and retention, playback
  previews in `uav-ground-station/.env`

If something does not work after a change, start with the common
[Troubleshooting](./how-to-guides/troubleshooting.md) guide; each page above
only lists the symptoms specific to it.


<!--hide_directive
:::{toctree}
:hidden:

Configure the Cameras <./how-to-customize/camera-management/configure-cameras.md>
Intel RealSense Camera <./how-to-customize/camera-management/realsense-guide.md>
Manage the Detection Model <./how-to-customize/model-and-video-management/model.md>
Modify the Detection Model <./how-to-customize/model-and-video-management/modify-model.md>
Change the Captioning (VLM) Model <./how-to-customize/model-and-video-management/vlm-model.md>
Change the Video Source <./how-to-customize/model-and-video-management/change-video-source.md>
Use Your Own Videos <./how-to-customize/model-and-video-management/use-your-own-video.md>
Manage Pipelines <./how-to-customize/pipeline-management/manage-pipelines.md>
Modify Pipelines <./how-to-customize/pipeline-management/modify-pipelines.md>
Customize the Telemetry Overlay <./how-to-customize/telemetry-overlay/customize-telemetry-overlay.md>
Tune VSS Live Search <./how-to-customize/search-tuning/vss-search-tuning.md>
:::
hide_directive-->

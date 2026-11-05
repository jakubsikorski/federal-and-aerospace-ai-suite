<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Modify the Detection Model

How to run detection with a model other than the bundled YOLO11s — another
Ultralytics size, or your own fine-tuned detector — and point one or more
cameras at it. See [Manage the Detection Model](./model.md) for the bundled
model and how it is produced.

## What the pipelines accept

`gvadetect` loads an **OpenVINO IR** model (`.xml` + `.bin`). The model file
must live below `resources/models/` (the pipeline schema rejects any other
path), and a camera refers to it by its path relative to that folder. PyTorch
or ONNX models must be converted first.

## Option A — Another Ultralytics model

Use the virtual environment `make model` created to export any Ultralytics
detection model (`yolo11n`, `yolo11m`, `yolov8s`, ...) with the same options
the bundled export uses:

```bash
mkdir -p resources/models/yolo11m && cd resources/models/yolo11m
../../venv/bin/yolo export model=yolo11m.pt format=openvino dynamic=True opset=18 imgsz=640 half=True
cd -
```

This downloads `yolo11m.pt` and writes
`resources/models/yolo11m/yolo11m_openvino_model/yolo11m.xml`. Keep the
pinned `ultralytics==8.4.67`; newer versions export a graph that fails on
GPU and NPU (see [Manage the Detection Model](./model.md)).

## Option B — A custom or fine-tuned model

1. Convert to OpenVINO IR if needed:

   ```bash
   resources/venv/bin/yolo export model=/path/to/best.pt format=openvino dynamic=True opset=18 imgsz=640 half=True   # Ultralytics-trained
   # or, for other frameworks:
   resources/venv/bin/pip install "openvino>=2026.0" && resources/venv/bin/ovc /path/to/model.onnx --compress_to_fp16
   ```

2. Copy the `.xml` + `.bin` pair into a folder below `resources/models/`,
   e.g. `resources/models/my-detector/model.xml`.

Models exported from Ultralytics carry their class names and post-processing
in the IR's metadata, which `gvadetect` reads directly. A model from another
framework may need a model-proc JSON for its output layout; see the DL
Streamer documentation for `gvadetect model-proc=`.

## Point a camera at the model

Set `detection.model` for the camera in
[`configs/cameras-pymavlink.yaml`](../camera-management/configure-cameras.md)
(or `cameras-uavsdk.yaml`), relative to `resources/models`:

```yaml
cameras:
  - id: front_camera
    detection:
      device: gpu
      model: yolo11m/yolo11m_openvino_model/yolo11m.xml
```

```bash
make check-cameras    # fails if the file does not exist
make start-rtsp       # or disarm/arm; the file is re-read at every arming
```

Cameras may use different models; each device/model pair loads its own
model instance. The file is validated before anything starts, and a missing
model is reported as `cameras[front_camera]: detection model not found: ...`.

For a pipeline started by hand, pass the container path in
`parameters.detection_model` (see
[Manage Pipelines — Start a pipeline](../pipeline-management/manage-pipelines.md#start-a-pipeline)):

```json
"parameters": {"detection_model": "/home/pipeline-server/resources/models/yolo11m/yolo11m_openvino_model/yolo11m.xml"}
```

## Verify

```bash
make cameras-status
ffplay rtsp://<HOST_IP>:8555/uav-front-camera
```

Boxes and labels should reflect the new model's classes. The detection log
(`destination.metadata`, see
[View Detection Logs](../../how-to-guides/view-detection-logs.md)) shows the
labels as text.

## Troubleshooting

No detections with the new model (`threshold`), `400` from the REST API
(path outside `resources/models/`), a model that fails on GPU/NPU, and
`detection model not found` are covered in
[Troubleshooting — Models & Captioning](../../how-to-guides/troubleshooting.md#models--captioning).
A larger model may not keep up with 30 fps on the CPU — use the GPU or NPU,
or raise `inference-interval` in the pipeline string.

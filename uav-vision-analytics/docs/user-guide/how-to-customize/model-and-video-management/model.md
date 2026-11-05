<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Manage the Detection Model

The application detects objects with **YOLO11s**, Ultralytics' COCO-pretrained
small model, exported to OpenVINO IR by `make model`. This page covers the
bundled model, how it is produced, where it lives, how the pipelines find it
and how to re-export it. To use a different model, see
[Modify the Detection Model](./modify-model.md); for the scene-captioning
model, see [Change the Captioning Model](./vlm-model.md).

## The bundled model

| Property | Value |
| --- | --- |
| Model | YOLO11s (Ultralytics, COCO-pretrained) |
| Format | OpenVINO IR, FP16, dynamic shape, opset 18, 640 × 640 input |
| Classes | 80 COCO classes (person, car, truck, bus, bicycle, motorcycle, ...) |
| Export tool | `ultralytics==8.4.67` + `openvino>=2026.0` (`resources/requirements.txt`) |

> [!IMPORTANT]
> `ultralytics` is pinned to `8.4.67`. Releases from `8.4.115` export a
> detection head that compiles on CPU only and fails on the GPU and NPU
> plugins. Do not bump the pin without re-verifying the exported model on
> all three devices.

## Download and export (`make model`)

Prerequisites: Python 3.10+ with `python3-venv` (`sudo apt install
python3.12-venv` on Ubuntu 24.04), and internet access (set
`https_proxy`/`http_proxy` first behind a proxy).

```bash
make model
```

The target first shows the model's license and asks for confirmation
(`ACCEPT_LICENSE=y` skips the prompt, for scripts). It then creates
`resources/venv/`, installs `resources/requirements.txt`,
downloads `yolo11s.pt` and runs `yolo export format=openvino half=True
dynamic=True imgsz=640`. Output:

```text
resources/models/yolo11s/
├── yolo11s.pt                       # downloaded checkpoint
└── yolo11s_openvino_model/
    ├── yolo11s.xml                  # OpenVINO IR
    ├── yolo11s.bin
    └── metadata.yaml
```

`make pymav-up` / `make uavsdk-up` refuse to start until `yolo11s.xml`
exists.

## How the pipelines find the model

`resources/` is mounted read-only in the container at
`/home/pipeline-server/resources/`. Each camera names its model in
[`configs/cameras-pymavlink.yaml`](../camera-management/configure-cameras.md)
relative to `resources/models`:

```yaml
    detection:
      device: npu
      model: yolo11s/yolo11s_openvino_model/yolo11s.xml
```

The pipeline manager turns this into the pipeline's `detection_model`
parameter (`/home/pipeline-server/resources/models/<model>`) and a
`detection_instance_id` derived from device and model file, so cameras that
share a device and model share one loaded model. The pipelines' own
`default` for `detection_model` is the bundled YOLO11s path.

## Re-export

Delete the export and run `make model` again, for example after changing
`resources/requirements.txt` or to try another export option:

```bash
rm -rf resources/models/yolo11s/yolo11s_openvino_model
make model
```

For an INT8 export add `int8=True` to the `yolo export` call in the
`Makefile`'s `model` target (`nncf` is already in the requirements). Restart
the stack afterwards so running pipelines load the new files.

## Verify

```bash
ls resources/models/yolo11s/yolo11s_openvino_model/
make check-cameras         # reports a camera whose model file is missing
```

## Troubleshooting

`make model` failing on `python3-venv`, a model that loads on CPU but not on
GPU/NPU, and a missing model reported by `make check-cameras` are covered in
[Troubleshooting — Setup](../../how-to-guides/troubleshooting.md#make-model-fails--python3-venv-not-available)
and [Troubleshooting — Models & Captioning](../../how-to-guides/troubleshooting.md#models--captioning).

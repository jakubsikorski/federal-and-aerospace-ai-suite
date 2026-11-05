<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Enabling NPU Inference

NPU is a first-class supported inference device — it has its own pipeline
entries (`..._npu`) selectable per camera in `configs/cameras-pymavlink.yaml`
(`detection.device: npu`) — but it requires hardware detection at `make init`
time and has a couple of recognizable failure modes. This tutorial covers
**one-time hardware/driver enablement** end-to-end. Once done, switching a
camera to (or away from) the NPU is a one-line change in the camera file, see
[Configure the Cameras](../how-to-customize/camera-management/configure-cameras.md#which-pipeline-runs).

## Prerequisites

- An Intel NPU (e.g. Meteor Lake / Panther Lake) with the NPU driver
  installed on the host.
- The stack not yet started for this session (or willing to restart it).

## Steps

### 1. Confirm the NPU device node exists on the host

```bash
ls /dev/accel/
# expected: accel0 (or similar)
```

If this directory doesn't exist or is empty, the NPU driver isn't installed /
loaded on the host — install it before continuing (outside the scope of this
app).

### 2. Detect and wire it via `make init`

```bash
make init
```

`make init` scans `/dev/accel/` and writes the first match to `.env`:

```text
✅ NPU detected:
   NPU_DEVICE=/dev/accel/accel0
```

`make init` **skips entirely if `.env` already exists** — if you added the
NPU after your first `make init`, either delete `.env` and re-run it, or set
`NPU_DEVICE` manually:

```bash
echo "NPU_DEVICE=/dev/accel/accel0" >> .env
```

### 3. Verify the compose wiring

`docker-compose-pymavlink.yml` (and the UAVSDK compose file) already pass
`NPU_DEVICE` through as both a device mount and an environment variable:

```yaml
environment:
  - ZE_ENABLE_ALT_DRIVERS=libze_intel_npu.so
  - NPU_DEVICE=${NPU_DEVICE:-/dev/null}
devices:
  - ${NPU_DEVICE:-/dev/null}:${NPU_DEVICE:-/dev/null}
```

No manual edits are needed here — this is just what makes `NPU_DEVICE` from
`.env` reach the container. `ZE_ENABLE_ALT_DRIVERS` enables the NPU OpenVINO
plugin and is already set by default.

### 4. Restart the stack so the container picks up the device

```bash
make pymav-down && make pymav-up
```

### 5. Start a camera on the NPU

Set `detection.device: npu` for a camera in `configs/cameras-pymavlink.yaml`
(see [Configure the Cameras](../how-to-customize/camera-management/configure-cameras.md)), then:

```bash
make check-cameras
make start-rtsp
ffplay rtsp://<HOST_IP>:8555/uav-front-camera
```

## Verify

```bash
curl -k -s https://<HOST_IP>/pipelines/status | python3 -m json.tool
```

Confirm the `..._npu` pipeline shows `"state": "RUNNING"`, and that
`dmesg | grep -i npu` on the host shows no driver errors.

## Troubleshooting

`[pipeline] Skipping ... NPU_DEVICE not available`, a poisoned
`model-instance-id`, and the driver checks are covered in
[Troubleshooting — NPU inference fails](./troubleshooting.md#npu-inference-fails).
Until the NPU is detected, set the camera to `cpu` or `gpu`; `make
check-cameras` reports an NPU camera as unavailable rather than silently
falling back.

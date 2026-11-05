<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Use Your Own Videos

In standalone (pymavlink) mode each camera loops a video file from
`resources/videos/`. This page covers converting your own footage into the
format the pipelines expect and pointing a camera at it. UAV Mission Compute
SDK mode reads live RTSP streams from the SDK instead; replace the SDK's
simulated camera source per that project's documentation.

## The bundled files

| File | Default camera (`configs/cameras-pymavlink.yaml`) |
| --- | --- |
| `resources/videos/uav_sample_front.avi` | `front_camera` |
| `resources/videos/uav_sample_bottom.avi` | `bottom_camera` |
| `resources/videos/uav_sample_back.avi` | `back_camera` |

A camera's `video:` field names any file in `resources/videos` (`.avi`,
`.mp4`, `.mkv` or `.ts`; no sub-folders). The folder is mounted read-only in
the container, so a file added on the host is visible without a restart.

## Convert a video

`scripts/convert_video.sh` turns anything ffmpeg can read into the format
the pipelines are tuned for: H.264 (High profile, yuv420p) in an AVI
container, 30 fps, no audio, about 4 Mbps, a keyframe every 250 frames, and
trimmed to whole 250-frame groups so the loop point lands on a keyframe. It
uses the host's `ffmpeg` if installed, otherwise ffmpeg from the pipeline
server image, so no extra install is needed.

```bash
./scripts/convert_video.sh /path/to/my_clip.mp4 -o resources/videos/my_clip.avi
```

When replacing several cameras' files, give them the same frame count so
their loops stay in step (a multiple of 250, no longer than the shortest
clip):

```bash
./scripts/convert_video.sh front.mp4  -o resources/videos/uav_sample_front.avi  -n 1750 -f
./scripts/convert_video.sh bottom.mkv -o resources/videos/uav_sample_bottom.avi -n 1750 -f
./scripts/convert_video.sh back.mov   -o resources/videos/uav_sample_back.avi   -n 1750 -f
```

The script verifies its output and ends with `Verified: valid H.264/AVI,
starts on a keyframe, decodes without errors.`

| Option | Default | Purpose |
| --- | --- | --- |
| `-o, --output <file.avi>` | `<input name>.avi` | Output file |
| `-n, --frames <N>` | whole 250-frame groups | Write exactly N frames (same N for every camera) |
| `-b, --bitrate <rate>` | `4M` | Target bitrate (`4M`, `4000k`) |
| `-r, --fps <n>` | `30` | Output frame rate |
| `-s, --size <WxH>` | source size | Scale and letterbox, e.g. `1920x1080` |
| `-g, --gop <n>` | `250` | Keyframe interval |
| `-p, --preset <name>` | `medium` | x264 preset (`ultrafast` … `veryslow`) |
| `--no-trim` | off | Keep every frame instead of trimming to whole groups |
| `-f, --force` | off | Overwrite an existing output |

## Apply it

Either overwrite a bundled file (keeping its name, as in the example above)
or set `video:` for the camera:

```yaml
cameras:
  - id: front_camera
    video: my_clip.avi
```

```bash
make check-cameras      # reports "video not found" if the name is wrong
make start-rtsp         # or disarm/arm; the file is re-read at every arming
```

A pipeline that is already running keeps the file it opened; it picks up a
replaced file when it is restarted (next arm, or
[Manage Pipelines — Stop](../pipeline-management/manage-pipelines.md#stop-a-pipeline)).
For a pipeline started by hand, pass the file name in `parameters.video`.

## Verify

```bash
ffplay rtsp://<HOST_IP>:8555/uav-front-camera
```

## Troubleshooting

`scripts/convert_video.sh` error messages, `video not found` from
`make check-cameras`, a pipeline that errors on an unconverted file, and a
stream that still shows the old footage are covered in
[Troubleshooting — Cameras & Video](../../how-to-guides/troubleshooting.md#cameras--video).
A soft picture means a low-bitrate source; conversion cannot improve it.

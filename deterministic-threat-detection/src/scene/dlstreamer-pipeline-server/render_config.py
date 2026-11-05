#!/usr/bin/env python3
# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Render the DL Streamer Pipeline Server config for the chosen camera source.

  SOURCE=video   the synchronised test videos served by the camera machines (via the mediamtx relay); frames
                 carry an SEI frame number, so the live HOTA can be scored
  SOURCE=basler  Basler GigE cameras (BASLER_CAMERA1_SERIAL / BASLER_CAMERA2_SERIAL); uses the camera's PTP
                 hardware timestamp

Usage: render_config.py <output file> [env file]
Settings come from the environment, then from the env file (KEY=VALUE lines, see .env.example).
"""
import json
import os
import sys

MODEL = "/home/pipeline-server/models/omz/person-detection-retail-0013/FP32/person-detection-retail-0013.xml"
MODEL_PROC = "/home/pipeline-server/models/object_detection/person/person-detection-retail-0013.json"
SEI_PARSER = "/home/pipeline-server/user_scripts/gvapython/sscape/sei_parser.py"
ADAPTER = "/home/pipeline-server/user_scripts/gvapython/sscape/sscape_adapter.py"

# Defaults per source: frame size the cameras are calibrated for in the scene
DEFAULT_SIZE = {"video": (800, 600), "basler": (1920, 1080)}


FILE_ENV = {}


def load_env_file(path):
  with open(path, encoding="utf-8") as f:
    for line in f:
      line = line.strip()
      if line and not line.startswith("#") and "=" in line:
        key, value = line.split("=", 1)
        FILE_ENV[key.strip()] = value.strip()


def env(name, default=""):
  return os.environ.get(name) or FILE_ENV.get(name) or default


def head(source, n, width, height):
  """Pipeline from the camera up to (not including) the timestamp element."""
  caps = f"video/x-raw,format=BGR,width={width},height={height}"
  if source == "video":
    return (f"rtspsrc location=rtsp://mediamtx:8554/{env(f'CAMERA{n}_ID', f'Cam_x{n}_0')} latency=200 ! rtph264depay ! h264parse"
            f" ! gvapython class=SEIParser arg=[\"camera{n}\"] function=process module={SEI_PARSER} name=sei_parser"
            f" ! avdec_h264 ! videoconvert ! videoscale ! {caps}")
  serial = env(f"BASLER_CAMERA{n}_SERIAL")
  if not serial:
    sys.exit(f"SOURCE=basler needs BASLER_CAMERA{n}_SERIAL")
  return (f"gencamsrc serial={serial} pixel-format=bayerrggb frame-rate={env('BASLER_FPS', '15')} name=source"
          f" ! bayer2rgb ! videoscale ! video/x-raw,width={width},height={height} ! videoconvert ! video/x-raw,format=BGR")


def pipeline(source, n, width, height):
  def adapter(cls, *args):
    arg = f" arg={json.dumps(list(args)).replace(' ', '')}" if args else ""
    return f"gvapython class={cls}{arg} function=processFrame module={ADAPTER}"

  # Camera sources get their own thread per stage; a leaky queue drops stale frames instead of stalling the camera
  q = " ! queue max-size-buffers=2 leaky=downstream" if source == "basler" else ""
  return (f"{head(source, n, width, height)}{q} ! {adapter('PostDecodeTimestampCapture')}"
          f" ! gvadetect threshold={env('DETECT_THRESHOLD', '0.4')} model={MODEL} model-proc={MODEL_PROC}"
          f"{q} ! gvametaconvert add-tensor-data=true name=metaconvert"
          f" ! {adapter('PostInferenceDataPublish', env(f'CAMERA{n}_ID', f'Cam_x{n}_0'))}"
          " ! gvametapublish name=destination method=file file-path=/dev/null ! appsink sync=true")


def render(source):
  if source not in DEFAULT_SIZE:
    sys.exit(f"unknown SOURCE '{source}' (video or basler)")
  width = int(env("FRAME_WIDTH", str(DEFAULT_SIZE[source][0])))
  height = int(env("FRAME_HEIGHT", str(DEFAULT_SIZE[source][1])))
  pipelines = [{
    "name": f"hota-cam{n}", "source": "gstreamer", "pipeline": pipeline(source, n, width, height), "auto_start": True}
    for n in (1, 2)]
  return {"config": {"logging": {"C_LOG_LEVEL": "INFO", "PY_LOG_LEVEL": "INFO"}, "pipelines": pipelines}}


if __name__ == "__main__":
  if len(sys.argv) not in (2, 3):
    sys.exit(__doc__)
  if len(sys.argv) == 3:
    load_env_file(sys.argv[2])
  with open(sys.argv[1], "w", encoding="utf-8") as out:
    json.dump(render(env("SOURCE", "video")), out, indent=2)
    out.write("\n")

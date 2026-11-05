# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Camera configuration for the pipeline manager: load, validate and turn it into pipeline start requests.

One entry per camera says where its images come from (a video file, or a live camera such as an Intel
RealSense or a USB webcam, through its /dev/video node, or an RTSP stream), which device and model detect objects, whether the VLM
captions it (and on which device, with which model and prompt) and whether detections are drawn on the
stream. From that the manager picks one of the eighteen pipelines in ``configs/config-pymavlink.json``:

    video file     detection only          uav_object_detection_<det>             det: cpu | gpu | npu
                   detection + captioning  uav_object_detection_<det>_<vlm>      vlm: cpu | gpu
    live camera    detection only          uav_camera_<det>
                   detection + captioning  uav_camera_<det>_<vlm>

This module has no dependency on MAVLink or on the manager, so a REST endpoint or the web UI can reuse the
same validation (``CamerasConfig``), the JSON schema (``json_schema()``) and the plan (``build_plan()``).
The file is YAML (JSON is valid YAML, so ``.json`` files load too).

CLI:
    camera_config.py check [FILE]     validate and print what would run (no hardware needed beyond the file checks)
    camera_config.py plan  [FILE]     the same plan as JSON, including the start requests
    camera_config.py schema           JSON schema of the configuration (for UI forms)
"""
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import asdict, dataclass
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

SCHEMA_VERSION = 1
RESOURCES_DIR = os.getenv("PIPELINE_RESOURCES_DIR", "/home/pipeline-server/resources")
DEFAULT_FILE = os.getenv("CAMERAS_CONFIG", "/home/pipeline-server/configs/cameras-pymavlink.yaml")
PIPELINES_FILE = os.getenv("PIPELINES_CONFIG", "/home/pipeline-server/config.json")

# The pipelines accept these values only through schema patterns, so keep the same rules here.
_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_VIDEO = re.compile(r"^[A-Za-z0-9_.-]+\.(avi|mp4|mkv|ts)$")
_REL_MODEL = re.compile(r"^[A-Za-z0-9_./-]+\.xml$")
_VLM_MODEL = re.compile(r"^[A-Za-z0-9_.-]+$")
_PROMPT = re.compile(r"^[A-Za-z0-9 ,.?'-]+$")
_RTSP = re.compile(r"^rtsp://[A-Za-z0-9_.-]{1,128}(:[0-9]{1,5})?/[A-Za-z0-9_./{}-]{1,128}$")
_UAV_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_DEVICE = re.compile(r"^/dev/(video[0-9]{1,3}|v4l/by-(id|path)/[A-Za-z0-9_.:+-]{1,128})$")
_PATH = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
_TOPIC = re.compile(r"^uav/caption/[A-Za-z0-9_-]{1,48}$")

Device = Literal["cpu", "gpu", "npu"]
VlmDevice = Literal["cpu", "gpu"]


class ConfigError(Exception):
    """The configuration is not usable; the message lists every problem, one per line."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")      # a misspelled key is an error, not silently ignored


class Detection(_Strict):
    device: Device = "npu"
    # relative to <resources>/models; the pipeline server only accepts models below that folder
    model: str = "yolo11s/yolo11s_openvino_model/yolo11s.xml"

    @field_validator("model")
    @classmethod
    def _model(cls, value: str) -> str:
        if not _REL_MODEL.match(value) or ".." in value.split("/") or value.startswith("/"):
            raise ValueError("must be a relative path to an .xml file below resources/models (letters, digits, _ . / -)")
        return value


class Captioning(_Strict):
    enabled: bool = False
    device: VlmDevice = "gpu"                       # the VLM runs on cpu or gpu only
    model: str = "InternVL2-1B"                      # folder name below <resources>/ov_models/<device>/
    prompt: str = "Describe what you see in the scene."
    max_new_tokens: int = Field(default=50, ge=1, le=4096)

    @field_validator("model")
    @classmethod
    def _model(cls, value: str) -> str:
        if not _VLM_MODEL.match(value) or value in (".", ".."):
            raise ValueError("must be a model folder name (letters, digits, _ . -)")
        return value

    @field_validator("prompt")
    @classmethod
    def _prompt(cls, value: str) -> str:
        if not _PROMPT.match(value):
            raise ValueError("may contain only letters, digits, spaces and , . ? ' -")
        return value


class Camera(_Strict):
    id: str
    enabled: bool = True
    # exactly one source: a video file below <resources>/videos (default uav_sample_front.avi) or a live camera
    video: Optional[str] = None
    rtsp: Optional[str] = None                        # an RTSP stream, e.g. from the UAV Mission Compute SDK; {uav_id} = env UAV_ID
    device: Optional[str] = None                      # /dev/videoN, or /dev/v4l/by-id|by-path/<name> (stable across reboots)
    width: Optional[int] = Field(default=None, ge=16, le=7680)     # live camera only; default 1280
    height: Optional[int] = Field(default=None, ge=16, le=4320)    # live camera only; default 720
    fps: Optional[int] = Field(default=None, ge=1, le=120)         # live camera only; default 30. Without it the camera may
                                                                   # settle on a low frame rate (5 fps) when captions are on
    overlay: bool = True                              # False hides the detection boxes AND the telemetry text
    detection: Detection = Detection()
    captioning: Captioning = Captioning()
    rtsp_path: Optional[str] = None                   # default: uav-<id with _ replaced by ->
    mqtt_topic: Optional[str] = None                  # default: uav/caption/<id>

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not _ID.match(value):
            raise ValueError("must start with a lowercase letter and contain only a-z, 0-9 and _ (max 32)")
        return value

    @field_validator("video")
    @classmethod
    def _video(cls, value):
        if value is not None and not _VIDEO.match(value):
            raise ValueError("must be a file name like uav_sample_front.avi (avi, mp4, mkv or ts, no folders)")
        return value

    @field_validator("device")
    @classmethod
    def _device(cls, value):
        if value is not None and not _DEVICE.match(value):
            raise ValueError("must be /dev/videoN or /dev/v4l/by-id/<name> or /dev/v4l/by-path/<name>")
        return value

    @field_validator("rtsp")
    @classmethod
    def _rtsp(cls, value):
        if value is None:
            return value
        if not _RTSP.match(value) or any(token != "{uav_id}" for token in re.findall(r"\{[^}]*\}|[{}]", value)):
            raise ValueError("must look like rtsp://host:8554/uav-1/nadir (no user or password; {uav_id} stands for the UAV id)")
        return value

    @model_validator(mode="after")
    def _source(self):
        if sum(value is not None for value in (self.video, self.device, self.rtsp)) > 1:
            raise ValueError("set only one of 'video' (a file), 'device' (a live camera) and 'rtsp' (a stream)")
        if self.device is None:
            if self.width is not None or self.height is not None or self.fps is not None:
                raise ValueError("'width', 'height' and 'fps' apply to a live camera ('device') only")
            if self.video is None and self.rtsp is None:
                self.video = "uav_sample_front.avi"
        return self

    @field_validator("rtsp_path")
    @classmethod
    def _rtsp_path(cls, value):
        if value is not None and not _PATH.match(value):
            raise ValueError("may contain only letters, digits, _ and - (max 48)")
        return value

    @field_validator("mqtt_topic")
    @classmethod
    def _topic(cls, value):
        if value is not None and not _TOPIC.match(value):
            raise ValueError("must look like uav/caption/<name> (name: letters, digits, _ and -, max 48)")
        return value


class CamerasConfig(_Strict):
    version: int = SCHEMA_VERSION
    cameras: list[Camera] = Field(min_length=1)

    @field_validator("version")
    @classmethod
    def _version(cls, value: int) -> int:
        if value != SCHEMA_VERSION:
            raise ValueError(f"unsupported version {value} (this software reads version {SCHEMA_VERSION})")
        return value

    @model_validator(mode="after")
    def _unique(self):
        for field, label in (("id", "camera id"), ("stream_path", "RTSP path"), ("device", "camera device")):
            seen = {}
            for camera in self.cameras:
                key = {"id": camera.id, "stream_path": stream_path(camera), "device": camera.device}[field]
                if key is None:
                    continue
                if key in seen:
                    raise ValueError(f"duplicate {label} '{key}' (cameras '{seen[key]}' and '{camera.id}')")
                seen[key] = camera.id
        return self


# ── derived values ────────────────────────────────────────────────────────────

def stream_path(camera: Camera) -> str:
    return camera.rtsp_path or "uav-" + camera.id.replace("_", "-")


def pipeline_name(camera: Camera) -> str:
    det = camera.detection.device
    prefix = "uav_camera" if camera.device else "uav_object_detection"
    return f"{prefix}_{det}_{camera.captioning.device}" if camera.captioning.enabled else f"{prefix}_{det}"


@dataclass(frozen=True)
class CameraPlan:
    """What the manager runs for one enabled camera."""
    camera_id: str
    pipeline: str
    rtsp_path: str
    mqtt_topic: Optional[str]
    source_url: Optional[str]     # RTSP source to probe before starting (None for files and local cameras)
    overlay: bool
    summary: str
    request: dict           # body of POST /pipelines/user_defined_pipelines/<pipeline>


def uav_id() -> str:
    value = os.getenv("UAV_ID", "uav-1")
    if not _UAV_ID.match(value):
        raise ConfigError(f"UAV_ID '{value}' must be letters, digits, _ and - (max 32)")
    return value


def _device_problem(device: str) -> Optional[str]:
    try:
        mode = os.stat(device).st_mode
    except OSError:
        return (f"camera device {device} not found (is the camera plugged in and its /dev node visible in the "
                "container? list the nodes with v4l2-ctl --list-devices)")
    if not stat.S_ISCHR(mode):
        return f"{device} is not a camera device"
    return None


def build_plan(config: CamerasConfig, *, resources_dir: str = RESOURCES_DIR, check_files: bool = True,
               thickness: str = "6", unavailable: Optional[list] = None,
               pipelines: Optional[set] = None) -> list[CameraPlan]:
    """Turn the enabled cameras into start requests. Raises ConfigError if a referenced file is missing.

    A live camera whose device is not there is not an error when ``unavailable`` (a list) is given: the camera is
    left out of the plan and the reason is appended to the list, so the other cameras still start.
    ``pipelines`` (names defined in the pipeline server's config) makes a camera that needs a missing pipeline an error."""
    problems, plans = [], []
    for camera in config.cameras:
        if not camera.enabled:
            continue
        det, cap = camera.detection, camera.captioning
        model_path = f"{resources_dir}/models/{det.model}"
        label = f"{camera.id.replace('_', '-')}-{det.device.upper()}" + (f"-{cap.device.upper()}-Caption" if cap.enabled else "")
        rtsp_url = camera.rtsp.replace("{uav_id}", uav_id()) if camera.rtsp else None
        parameters = {
            **({"rtsp_url": rtsp_url} if rtsp_url else {}),
            **({"device": camera.device, "camera_width": camera.width or 1280, "camera_height": camera.height or 720,
                                          "camera_fps": camera.fps or 30}
               if camera.device else {} if rtsp_url else {"video": camera.video}),
            "detection_model": model_path,
            # cameras sharing device and model path share one loaded model; a different model (even with the same file name) gets its own instance
            "detection_instance_id": re.sub(r"[^a-z0-9_]", "_", f"inst{det.device}_{os.path.splitext(os.path.basename(det.model))[0]}_{hashlib.sha256(os.path.normpath(det.model).encode()).hexdigest()[:8]}".lower()),
            "overlay_label": label,
        }
        topic = None
        if cap.enabled:
            topic = camera.mqtt_topic or f"uav/caption/{camera.id}"
            vlm_path = f"{resources_dir}/ov_models/{cap.device}/{cap.model}"
            parameters.update({"captioner_model_path": vlm_path, "captioner_prompt": cap.prompt,
                               "captioner_max_new_tokens": cap.max_new_tokens, "mqtt_topic": topic,
                               # cameras with the same device, model and token limit share one loaded VLM (the limit is a per-instance setting)
                               "captioner_instance_id": re.sub(r"[^a-z0-9_]", "_", f"instvlm_{cap.device}_{cap.model}_t{cap.max_new_tokens}".lower())})
        frame = {"type": "rtsp", "path": stream_path(camera), "encoding": "h264", "overlay-properties": {"thickness": thickness}}
        if not camera.overlay:
            frame["overlay"] = False
        if pipelines is not None and pipeline_name(camera) not in pipelines:
            problems.append(f"cameras[{camera.id}]: pipeline {pipeline_name(camera)} is not in the pipeline server's config.json"
                            + (" (copy it from configs/config-pymavlink-realsense.json)" if camera.device else ""))
        if check_files and camera.device:
            reason = _device_problem(camera.device)
            if reason and unavailable is not None:
                unavailable.append(f"cameras[{camera.id}]: {reason}")
                continue
            if reason:
                problems.append(f"cameras[{camera.id}]: {reason}")
        if check_files:
            for what, path in (*((("video", f"{resources_dir}/videos/{camera.video}"),) if camera.video else ()),
                               ("detection model", model_path),
                               *((("captioning model", vlm_path),) if cap.enabled else ())):
                if not os.path.exists(path):
                    problems.append(f"cameras[{camera.id}]: {what} not found: {path}")
        source = camera.device or rtsp_url or camera.video
        summary = f"{source}: {det.device.upper()} {os.path.basename(det.model)}" + (f" + {cap.device.upper()} {cap.model}" if cap.enabled else " (no captions)")
        plans.append(CameraPlan(camera.id, pipeline_name(camera), stream_path(camera), topic, rtsp_url, camera.overlay, summary,
                                {"destination": {"frame": frame}, "parameters": parameters}))
    if problems:
        raise ConfigError("\n".join(problems))
    return plans


# ── loading ───────────────────────────────────────────────────────────────────

def _format_errors(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        where = ".".join(str(part) for part in err["loc"]) or "(file)"
        lines.append(f"{where}: {err['msg'].removeprefix('Value error, ')}")
    return "\n".join(lines)


def parse_config(data) -> CamerasConfig:
    """Validate an already parsed document (the web UI / REST API passes its JSON body here)."""
    if not isinstance(data, dict):
        raise ConfigError("(file): the configuration must be a mapping with a 'cameras' list")
    try:
        return CamerasConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_errors(exc)) from None


def load_pipeline_names(path: str = PIPELINES_FILE) -> Optional[set]:
    """Names of the pipelines the pipeline server knows, or None if its config cannot be read."""
    try:
        with open(path, encoding="utf-8") as handle:
            return {p["name"] for p in json.load(handle)["config"]["pipelines"]}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def load_config(path: str = DEFAULT_FILE) -> CamerasConfig:
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from None
    return parse_config(data)


def json_schema() -> dict:
    return CamerasConfig.model_json_schema()


# ── command line ──────────────────────────────────────────────────────────────

def _table(plans: list[CameraPlan]) -> str:
    rows = [("camera", "pipeline", "stream path", "overlay", "source: detection / captioning")]
    rows += [(p.camera_id, p.pipeline, p.rtsp_path, "on" if p.overlay else "off", p.summary) for p in plans]
    widths = [max(len(row[i]) for row in rows) for i in range(5)]
    return "\n".join("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in rows)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv else "check"
    path = argv[1] if len(argv) > 1 else DEFAULT_FILE
    if command == "schema":
        print(json.dumps(json_schema(), indent=2))
        return 0
    if command not in ("check", "plan"):
        print(__doc__)
        return 2
    try:
        config = load_config(path)
        unavailable = []
        plans = build_plan(config, unavailable=unavailable, pipelines=load_pipeline_names())
    except ConfigError as exc:
        print(f"{path}: NOT valid\n" + "\n".join("  " + line for line in str(exc).splitlines()), file=sys.stderr)
        return 1
    if command == "plan":
        print(json.dumps([asdict(p) for p in plans], indent=2))
        return 0
    disabled = [c.id for c in config.cameras if not c.enabled]
    print(f"{path}: valid, {len(plans)} camera(s) enabled" + (f", disabled: {', '.join(disabled)}" if disabled else ""))
    print(_table(plans))
    for line in unavailable:
        print(f"WARNING {line}; left out", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

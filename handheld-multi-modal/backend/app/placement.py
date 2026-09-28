"""Read-only model placement inventory; configuration is not execution evidence."""
from __future__ import annotations

import copy
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shlex
import threading
import time
from urllib.parse import urlsplit

import httpx
import yaml


class Placement:
    def __init__(self, services, cfg, directory="/config/model-placement"):
        self.services = services
        self.host = cfg.get("execution_host", "Local edge host")
        self.directory = Path(directory)
        self.lock = threading.Lock()
        self.cached = None
        self.expires = 0.0

    def _configured(self, service):
        filename = "dlsps.json" if service == "dlsps" else service.replace("_", "-") + ".yaml"
        body = yaml.safe_load((self.directory / filename).read_text())
        if service == "chatqna":
            models, devices = body["model_settings"], body["device_settings"]
            return models["LLM_MODEL_ID"], devices["LLM_DEVICE"], [
                {"label": name.title(), "model": models[name + "_MODEL_ID"],
                 "device": devices[name + "_DEVICE"]}
                for name in ("EMBEDDING", "RERANKER")]
        if service in ("tts", "audio_analyzer"):
            model = body["models"]["tts" if service == "tts" else "asr"]
            # make TTS_DEVICE overrides the file through the stock environment variable.
            device = (os.environ.get("TTS_DEVICE") if service == "tts" else None) or model["device"]
            return model["name"], device, []
        from .pipelines import PIPELINE
        definition = next(p for p in body["config"]["pipelines"] if p["name"] == PIPELINE.split("/")[-1])
        pipeline = definition["pipeline"]
        detection = next(part for part in pipeline.split("!") if "gvadetect " in part)
        properties = dict(token.split("=", 1) for token in shlex.split(detection) if "=" in token)
        model = Path(properties["model"])
        stages = []
        if "gvatrack " in pipeline:
            stages.append({"label": "Tracking", "model": "gvatrack", "device": "CPU"})
        if "vah264dec" in pipeline:
            stages.append({"label": "Video decode", "model": "VA H.264", "device": "GPU"})
        stages.append({"label": "Video encode", "model": "DLSPS WebRTC encoder",
                       "device": "Not reported", "note": "GPU H.264 was verified in deployment evidence; runtime encoder selection is not exposed by this API."})
        return f"{model.stem} · {model.parent.name} · OpenVINO IR", properties["device"], stages

    def _read(self, item):
        service, label = item
        row = dict(id=service, label=label, host=self.host,
                   service_host=urlsplit(self.services.urls[service]).hostname,
                   configured_model=None, configured_device=None, stages=[],
                   runtime_model=None, runtime_device=None, status="unknown",
                   source="", note="", checked_utc=None)
        try:
            row["configured_model"], row["configured_device"], row["stages"] = self._configured(service)
        except (OSError, ValueError, KeyError, TypeError, StopIteration, yaml.YAMLError):
            row["note"] = "Deployment configuration unavailable. "
        try:
            if service == "dlsps":
                row["source"] = "DLSPS /pipelines/status + owned camera observations"
                body = self.services._get(service, "/pipelines/status").json()
                if not isinstance(body, list):
                    raise ValueError("invalid pipeline status")
                row["instances"] = {p["id"]: p["state"] for p in body}
                row["status"] = "unknown"  # readiness is scoped to Backend-owned instances below
            elif service == "chatqna":
                row["source"] = "Chat Q&A /model"
                body = self.services._get(service, "/model").json()
                row["runtime_model"] = body.get("llm_model")
                row["status"] = "ready" if row["runtime_model"] else "unknown"
            elif service == "tts":
                row["source"] = "TTS /v1/model-info"
                body = self.services._get(service, "/v1/model-info").json()
                row["runtime_model"] = body.get("model")
                row["runtime_device"] = body.get("device")
                row["status"] = "ready" if row["runtime_model"] and row["runtime_device"] else "unknown"
            else:
                row["source"] = "Audio Analyzer /health"
                _, body = self.services.audio_analyzer()
                row["status"] = "ready"
            if any(value is not None and not isinstance(value, str)
                   for value in (row["runtime_model"], row["runtime_device"])):
                row["runtime_model"] = row["runtime_device"] = None
                raise ValueError("invalid model metadata")
            for field in ("model", "device"):
                actual, configured = row["runtime_" + field], row["configured_" + field]
                if actual and configured and actual != configured:
                    row["status"] = "mismatch"
            row["note"] += ("Device reported by the service; not per-request execution proof."
                            if row["runtime_device"] else "Device is configured only; this API does not report the execution device.")
        except httpx.HTTPError:
            row["status"] = "unavailable"
            row["note"] += "Service unreachable or returned an HTTP error. Configured placement is retained."
        except (ValueError, KeyError, TypeError, AttributeError, RuntimeError):
            row["status"] = "unknown"
            row["note"] += "Service response does not establish readiness or placement."
        row["checked_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return row

    def snapshot(self, owned, feeds):
        # One shared ten-second cache prevents each browser probing every service.
        with self.lock:
            if self.cached is None or time.monotonic() >= self.expires:
                items = [("dlsps", "Vision"), ("chatqna", "LLM"),
                         ("audio_analyzer", "Transcription"), ("tts", "Speech")]
                with ThreadPoolExecutor(max_workers=4) as pool:
                    self.cached = list(pool.map(self._read, items))
                self.expires = time.monotonic() + 10
            rows = copy.deepcopy(self.cached)
        vision = rows[0]
        instances = vision.pop("instances", None)
        if instances is None:
            observed = [stream for stream, instance in owned.items()
                        if instance and feeds.get(stream, {}).get("ok") is True]
            if observed:
                vision["status"] = "unknown"
                vision["note"] += f" {len(observed)} owned cameras still have fresh observations; pipeline status could not be checked."
        else:
            ready = [stream for stream, instance in owned.items()
                     if instances.get(instance) == "RUNNING" and feeds.get(stream, {}).get("ok") is True]
            vision["status"] = "ready" if ready else "unavailable"
            vision["note"] += f" {len(ready)} of {len(owned)} cameras have a running pipeline and fresh observations."
        return {"models": rows, "refresh_seconds": 10}

#!/usr/bin/env python3
"""Ask every running service to do real work: video, detection, LLM, speech, transcription.

Used by `make check` once the application is started. Read-only apart from one
stateless /chat request, one synthesis and one transcription; exits 1 on failure.
"""
import json
import os
from pathlib import Path
import re
import ssl
import sys
import time
import urllib.request
import uuid

REPO = Path(__file__).resolve().parents[1]
ASSET_ROOT = Path(os.environ.get("ASSET_ROOT", REPO))
# Local calls must not go through an inherited corporate proxy.
OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
# A service still loading its model is reported, not waited for; demo-up waits.
INFERENCE_TIMEOUT = 60
# Cameras whose demo publisher runs (make demo-up); they must be live. Other
# cameras may simply not publish yet (make up), which is not a failure.
DEMO_CAMERAS = set(filter(None, os.environ.get("CHECK_DEMO_CAMERAS", "").split(",")))
failed = False


class Waiting(Exception):
    """Nothing is broken; an input the operator provides (a camera) is absent."""


def call(url, body=None, headers=None, timeout=5, raw=False):
    request = urllib.request.Request(url, data=body, headers=headers or {})
    with OPENER.open(request, timeout=timeout) as response:
        data = response.read()
    return data if raw else json.loads(data)


def probe(label, fix, test):
    """Run one probe; test returns a short detail string or raises."""
    global failed
    start = time.monotonic()
    try:
        detail = test()
        print(f"ok       {label}: {detail} ({time.monotonic() - start:.1f} s)", flush=True)
        return True
    except Waiting as reason:
        print(f"--       {label}: {reason}", flush=True)
        return True
    except Exception as error:  # every failure is reported, never hidden
        failed = True
        print(f"FAILED   {label}: {error}  -> {fix}", flush=True)
        return False


def configured(path, pattern, default=None):
    match = re.search(pattern, path.read_text(), re.MULTILINE) if path.is_file() else None
    return match.group(1) if match else default


def main():
    streams = [s for s in call("https://127.0.0.1:8443/api/streams")["data"] if s.get("enabled", True)]
    paths = {item["name"]: item.get("ready") for item in call("http://127.0.0.1:9997/v3/paths/list")["items"]}

    publishing = [s for s in streams if paths.get(s["stream_id"])]

    def camera(stream):
        def test():
            camera_id = stream["stream_id"]
            if not paths.get(camera_id):
                message = f"nothing published to rtsp://127.0.0.1:8554/{camera_id}"
                if camera_id in DEMO_CAMERAS:
                    raise RuntimeError(f"demo publisher runs but {message}")
                raise Waiting(f"waiting for a camera: {message}")
            feed = stream.get("feed") or {}
            if not feed.get("ok"):
                raise RuntimeError(f"camera publishes but no fresh detections ({feed.get('reason') or 'feed not receiving'})")
            annotated = camera_id.replace("cam", "annotated", 1)
            if not paths.get(annotated):
                raise RuntimeError(f"detections arrive but {annotated} video is not publishing")
            return f"camera feed, detections and {annotated} video live"
        return test

    for stream in streams:
        probe(f"{stream['stream_id']} {stream.get('display_name', '')}".strip(),
              "check the camera publisher, then make pipelines-restart", camera(stream))

    def dlsps():
        running = [p for p in call("http://127.0.0.1:8080/pipelines/status") if p.get("state") == "RUNNING"]
        if not publishing and not running:
            raise Waiting("no camera publishes yet; the Backend starts a pipeline when one does")
        if len(running) < len(publishing):
            raise RuntimeError(f"{len(running)} pipelines running for {len(publishing)} publishing cameras")
        stalled = [p for p in running if not p.get("avg_fps")]
        if stalled:
            raise RuntimeError(f"{len(stalled)} running pipelines process no frames")
        return "pipelines at " + ", ".join(f"{p['avg_fps']:.0f}" for p in running) + " fps"
    probe("DLSPS detection", "make pipelines-restart", dlsps)

    def metrics():
        data = call("https://127.0.0.1:8443/api/metrics")["data"]
        if data.get("status") != "ok" or not data.get("metrics"):
            raise RuntimeError(f"telemetry {data.get('status')}: {data.get('reason')}")
        return f"{len(data['metrics'])} live metrics from {data.get('host')}"
    probe("Metrics Manager telemetry", "docker logs handheld-multi-modal-metrics", metrics)

    def chat():
        expected = configured(REPO / "config/chatqna/config.yaml", r'^\s*LLM_MODEL_ID:\s*"([^"]+)"')
        loaded = call("http://127.0.0.1:8888/model").get("llm_model")
        if loaded != expected:
            raise RuntimeError(f"loaded model {loaded!r}, configured {expected!r}")
        result = call("http://127.0.0.1:8888/chat", json.dumps(
            {"input": "What is two plus two? Reply with one number.", "stream": False}).encode(),
            {"Content-Type": "application/json"}, timeout=INFERENCE_TIMEOUT)
        answer = str(result.get("metadata", "")).rsplit("Answer:", 1)[-1].strip()
        if result.get("status") != "Success" or not answer:
            raise RuntimeError(f"no answer: {result}")
        return f"{loaded} answered {answer[:40]!r}"
    probe("Chat Q&A LLM", "docker logs handheld-multi-modal-chatqna-core-gpu", chat)

    speech = {}

    def tts():
        expected = configured(ASSET_ROOT / "config/devices.yaml",
                              r"TEXT_TO_SPEECH__MODELS__TTS__DEVICE:\s*(\w+)", "CPU")
        info = call("http://127.0.0.1:8012/v1/model-info")
        if info.get("device") != expected:
            raise RuntimeError(f"runs on {info.get('device')}, configured {expected}")
        audio = call("http://127.0.0.1:8012/v1/audio/speech", json.dumps(
            {"input": "Camera two is ready.", "response_format": "wav"}).encode(),
            {"Content-Type": "application/json"}, timeout=INFERENCE_TIMEOUT, raw=True)
        if audio[:4] != b"RIFF" or len(audio) < 1000:
            raise RuntimeError(f"no WAV audio returned ({len(audio)} bytes)")
        speech["wav"] = audio
        return f"{info.get('model')} on {info['device']} spoke {len(audio) // 1024} KB of audio"
    probe("SpeechT5 text to speech", "docker logs handheld-multi-modal-tts", tts)

    def whisper():
        if call("http://127.0.0.1:8011/health").get("status") != "ok":
            raise RuntimeError("health is not ok")
        if "wav" not in speech:
            raise RuntimeError("no speech sample to transcribe (SpeechT5 failed)")
        boundary = uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nwhisper-1\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"check.wav\"\r\n"
                "Content-Type: audio/wav\r\n\r\n").encode() + speech["wav"] + f"\r\n--{boundary}--\r\n".encode()
        text = call("http://127.0.0.1:8011/v1/audio/transcriptions", body,
                    {"Content-Type": f"multipart/form-data; boundary={boundary}"}, timeout=INFERENCE_TIMEOUT).get("text", "")
        if not text.strip():
            raise RuntimeError("empty transcription")
        return f"transcribed the SpeechT5 sample as {text.strip()!r}"
    probe("Whisper transcription", "docker logs handheld-multi-modal-audio-analyzer", whisper)

    def alerts():
        status = call("http://127.0.0.1:8010/api/v1/health").get("status")
        if status != "healthy":
            raise RuntimeError(f"status {status}")
        return "rule dispatch healthy"
    probe("Alert Agent", "docker logs handheld-multi-modal-alert-agent", alerts)

    def console():
        page = call("https://127.0.0.1:8443/", raw=True).decode(errors="replace")
        if 'id="pause-updates"' not in page:
            raise RuntimeError("operator console page is not served")
        return "operator console served through the TLS proxy"
    probe("Operator console", "make up or make demo-up", console)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except OSError as error:
        sys.exit(f"FAILED   application is not reachable: {error}  -> make up or make demo-up")

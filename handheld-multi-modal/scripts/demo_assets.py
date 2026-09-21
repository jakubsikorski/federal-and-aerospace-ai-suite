#!/usr/bin/env python3
"""Prepare demo assets through stock Model Download. No pipeline lifecycle here."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import urllib.request
import uuid
import xml.etree.ElementTree as ET

REPO = Path(__file__).resolve().parents[1]
REQUEST = json.loads((REPO / "config/model-download/yolo11s.json").read_text())
IMAGE = "intel/model-download:2026.2.0-rc2@sha256:8894c3d624e949e79d54557b834a11a90bc3673a98de820a0297f3328b91cc95"
SOURCE = "c2cdea0ef9b137986b14e6bbeebcb80dbde4c0e9"
EXPORT_SCRIPT = "4d22f42ffe1df79800dc2ef1749f7e37f3d47bf1"
VIDEO_URL = f"https://raw.githubusercontent.com/open-edge-platform/edge-ai-libraries/{SOURCE}/microservices/dlstreamer-pipeline-server/resources/videos/classroom.avi"
VIDEO_SHA = "ca6d1b53d54a40ffda9d4ca1d0ab0cc43f744583324aaf97146c820e2b62a2fa"
LEGACY_HASHES = {
    "yolo11s.xml": "61e8501fa5a11fa012921d5f7c65f0ff05fa78f2d2bf0a37fc1fe7fdf8630d45",
    "yolo11s.bin": "8bb1f5d1b46de8c09fe26d0379072cfddf847e4d627696ccd59374b4a623d51d",
}
APPROVED_HASHES = (LEGACY_HASHES, {
    "yolo11s.xml": "fe835c8f856f48d43a2f7fe36ec5abbedded776a1d64e234a01e3e9e32d82d79",
    "yolo11s.bin": "5ece73c778c60c6164bc71cc8ca6b6ac1a413c91587d78d9202cf09cbde3613f",
})
FILES = (*LEGACY_HASHES, "metadata.yaml")
MODEL_DIR = Path("data/dlsps/yolo_export/yolo11s_int8_openvino_model")
API = "http://127.0.0.1:8200/api/v1"


def sha(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def verify_model(path):
    if not path.exists():
        return False
    for name in FILES:
        if not (path / name).is_file() or not (path / name).stat().st_size:
            raise ValueError(f"Incomplete model: {path / name}; preserve/remove the incomplete directory before retrying")
    hashes = {name: sha(path / name) for name in FILES}
    if not any(all(hashes[name] == value for name, value in approved.items())
               for approved in APPROVED_HASHES):
        raise ValueError(f"Unapproved YOLO11s IR hashes in {path}; review artifacts before changing pins")
    return True


def api(path, body=None):
    request = urllib.request.Request(API + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    # Local REST calls must not go through an inherited corporate proxy.
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=30) as response:
        return json.load(response)


def download_model(root, timeout):
    target = root / MODEL_DIR
    if verify_model(target):
        print(f"Verified cached YOLO11s INT8: {target}")
        return
    subdir = "yolo11s-" + uuid.uuid4().hex
    response = api("/models/download?download_path=" + subdir, REQUEST)
    job_id = str(uuid.UUID(response["job_ids"][0]))
    print(f"Model Download job {job_id}: YOLO11s INT8 / coco8", flush=True)
    deadline = time.monotonic() + timeout
    previous = None
    while True:
        job = api("/jobs/" + job_id)
        status = job.get("status")
        if status != previous:
            print(f"YOLO11s: {status}", flush=True)
            previous = status
        if status == "completed":
            break
        if status not in {"queued", "downloading", "converting"}:
            raise RuntimeError(f"Model Download job {job_id}: {job}")
        if time.monotonic() >= deadline:
            api(f"/jobs/{job_id}/cancel", {})
            raise TimeoutError(f"Model Download job {job_id} exceeded {timeout}s and was canceled")
        time.sleep(2)
    if (job.get("model_name") != "yolo11s" or job.get("hub") != "ultralytics"
            or job.get("result", {}).get("success") is not True
            or job.get("result", {}).get("int8_requested") is not True):
        raise ValueError(f"Unexpected completed job: {job}")
    source = root / "data/model-download" / subdir / "ultralytics/public/yolo11s/INT8"
    tree = ET.parse(source / "yolo11s.xml")
    if tree.find(".//rt_info/nncf/quantization") is None:
        raise ValueError("Downloader output has no INT8 quantization metadata")
    metadata = (source / "metadata.yaml").read_text()
    if "Ultralytics YOLO11s" not in metadata or "coco8.yaml" not in metadata:
        raise ValueError("Downloader output is not YOLO11s calibrated on coco8")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Publish only complete artifacts, never partially downloaded files.
    with tempfile.TemporaryDirectory(prefix=".yolo11s-", dir=target.parent) as temporary:
        staged = Path(temporary) / "model"
        staged.mkdir()
        for name in FILES:
            shutil.copyfile(source / name, staged / name)
        receipt = dict(image=IMAGE, source_commit=SOURCE, export_script_commit=EXPORT_SCRIPT,
                       request=REQUEST, job=job, files={name: sha(staged / name) for name in FILES})
        (staged / "download.json").write_text(json.dumps(receipt, indent=2) + "\n")
        verify_model(staged)
        staged.rename(target)
    print(f"Installed verified YOLO11s INT8: {target}")


def download_video(root):
    target = root / "data/dlsps/demo-video/classroom.avi"
    if target.exists():
        if sha(target) != VIDEO_SHA:
            raise ValueError(f"Demo video checksum mismatch: {target}; refusing to overwrite")
        print(f"Verified cached demo video: {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".video-", dir=target.parent) as temporary:
        staged = Path(temporary) / target.name
        with urllib.request.urlopen(VIDEO_URL, timeout=60) as response, staged.open("wb") as output:
            shutil.copyfileobj(response, output)
        if sha(staged) != VIDEO_SHA:
            raise ValueError("Downloaded demo video checksum mismatch")
        staged.rename(target)
    print(f"Downloaded pinned demo video: {target}")


def repair_chat_cache(directory, models):
    """Stock conversion skips existing directories, even interrupted exports."""
    files = ("openvino_model.xml", "openvino_model.bin", "openvino_tokenizer.xml",
             "openvino_tokenizer.bin", "config.json", "tokenizer_config.json")
    for model in models:
        path = directory / model
        if path.is_dir() and not all((path / name).is_file() and (path / name).stat().st_size for name in files):
            backup = path.with_name(path.name + ".incomplete-" + str(time.time_ns()))
            path.rename(backup)
            print("Preserved incomplete export:", backup)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check-model", "model", "video"])
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("ASSET_ROOT", REPO)))
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    with (args.root / ".demo-assets.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.action == "check-model":
            return 0 if verify_model(args.root / MODEL_DIR) else 2
        if args.action == "model":
            download_model(args.root, args.timeout)
        else:
            download_video(args.root)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, RuntimeError, ET.ParseError) as error:
        sys.exit(f"demo-assets: {error}")

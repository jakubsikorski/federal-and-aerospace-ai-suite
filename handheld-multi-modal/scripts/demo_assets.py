#!/usr/bin/env python3
"""Prepare demo assets through stock Model Download. No pipeline lifecycle here."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
import urllib.request
import uuid
# Parses only OpenVINO IR written locally by the pinned Model Download job;
# Python's expat does not expand external entities.
import xml.etree.ElementTree as ET  # nosec B405

REPO = Path(__file__).resolve().parents[1]
REQUEST = json.loads((REPO / "config/model-download/yolo26s.json").read_text())
MODEL = REQUEST["models"][0]["name"]
IMAGE = "intel/model-download:2026.2.0@sha256:8894c3d624e949e79d54557b834a11a90bc3673a98de820a0297f3328b91cc95"
SOURCE = "c2cdea0ef9b137986b14e6bbeebcb80dbde4c0e9"
EXPORT_SCRIPT = "4d22f42ffe1df79800dc2ef1749f7e37f3d47bf1"
VIDEO_URL = f"https://raw.githubusercontent.com/open-edge-platform/edge-ai-libraries/{SOURCE}/microservices/dlstreamer-pipeline-server/resources/videos/classroom.avi"
VIDEO_SHA = "ca6d1b53d54a40ffda9d4ca1d0ab0cc43f744583324aaf97146c820e2b62a2fa"
# (file name, source URL, sha256). Make re-encodes the mp4 clips to <stem>.maxN.ts
# for the camera publishers; classroom.avi serves the DLSPS smoke tests.
# Pexels items are used under the Pexels License.
VIDEOS = (
    ("classroom.avi", VIDEO_URL, VIDEO_SHA),
    # cam1: intel-iot-devkit/sample-videos (CC BY 4.0), H.264 768x432 12 fps, 53.9 s.
    ("person-bicycle-car-detection.mp4",
     "https://raw.githubusercontent.com/intel-iot-devkit/sample-videos/"
     "57978890822836f2b4743852f04f62fc511757e4/person-bicycle-car-detection.mp4",
     "452b11b7e0efbd019f1d9570d0c790e90416ad4ad29eec6003872d08443140ef"),
    # cam2: Pexels video 34622113, H.264 1920x1080 59.94 fps, 20.0 s.
    ("pexels-34622113.mp4", "https://www.pexels.com/download/video/34622113/?w=1920&h=1080",
     "ef856e96a33f035b9ab8d6ecbd0c6006c8e9dd98c177af1e7ea0bd0702d7172f"),
)
APPROVED_HASHES = tuple(json.loads(
    (REPO / "config/dlsps/detector-profile.json").read_text())["artifact_sets"])
FILES = (f"{MODEL}.xml", f"{MODEL}.bin", "metadata.yaml")
MODEL_DIR = Path(f"data/dlsps/yolo_export/{MODEL}_int8_openvino_model")
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
        raise ValueError(f"Unapproved {MODEL} IR hashes in {path}; review artifacts before changing pins")
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
        print(f"Verified cached {MODEL} INT8: {target}")
        return
    subdir = f"{MODEL}-" + uuid.uuid4().hex
    response = api("/models/download?download_path=" + subdir, REQUEST)
    job_id = str(uuid.UUID(response["job_ids"][0]))
    print(f"Model Download job {job_id}: {MODEL} INT8 / coco8", flush=True)
    deadline = time.monotonic() + timeout
    previous = None
    while True:
        job = api("/jobs/" + job_id)
        status = job.get("status")
        if status != previous:
            print(f"{MODEL}: {status}", flush=True)
            previous = status
        if status == "completed":
            break
        if status not in {"queued", "downloading", "converting"}:
            raise RuntimeError(f"Model Download job {job_id}: {job}")
        if time.monotonic() >= deadline:
            api(f"/jobs/{job_id}/cancel", {})
            raise TimeoutError(f"Model Download job {job_id} exceeded {timeout}s and was canceled")
        time.sleep(2)
    if (job.get("model_name") != MODEL or job.get("hub") != "ultralytics"
            or job.get("result", {}).get("success") is not True
            or job.get("result", {}).get("int8_requested") is not True):
        raise ValueError(f"Unexpected completed job: {job}")
    source = root / "data/model-download" / subdir / f"ultralytics/public/{MODEL}/INT8"
    tree = ET.parse(source / f"{MODEL}.xml")  # nosec B314 - local downloader output
    if tree.find(".//rt_info/nncf/quantization") is None:
        raise ValueError("Downloader output has no INT8 quantization metadata")
    metadata = (source / "metadata.yaml").read_text()
    if f"ultralytics {MODEL}" not in metadata.lower() or "coco8.yaml" not in metadata:
        raise ValueError(f"Downloader output is not {MODEL} calibrated on coco8")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Publish only complete artifacts, never partially downloaded files.
    with tempfile.TemporaryDirectory(prefix=f".{MODEL}-", dir=target.parent) as temporary:
        staged = Path(temporary) / "model"
        staged.mkdir()
        for name in FILES:
            shutil.copyfile(source / name, staged / name)
        receipt = dict(image=IMAGE, source_commit=SOURCE, export_script_commit=EXPORT_SCRIPT,
                       request=REQUEST, job=job, files={name: sha(staged / name) for name in FILES})
        (staged / "download.json").write_text(json.dumps(receipt, indent=2) + "\n")
        verify_model(staged)
        staged.rename(target)
    print(f"Installed verified {MODEL} INT8: {target}")


def download_video(root):
    for name, url, digest in VIDEOS:
        target = root / "data/dlsps/demo-video" / name
        if target.exists():
            if sha(target) != digest:
                raise ValueError(f"Demo video checksum mismatch: {target}; refusing to overwrite")
            print(f"Verified cached demo video: {target}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".video-", dir=target.parent) as temporary:
            staged = Path(temporary) / target.name
            # Pexels rejects urllib's default User-Agent with 403.
            request = urllib.request.Request(url, headers={"User-Agent": "handheld-demo-assets/1.0"})
            # Pinned https URL only; the download is sha256-verified below.
            with urllib.request.urlopen(request, timeout=60) as response, staged.open("wb") as output:  # nosec B310
                shutil.copyfileobj(response, output)
            if sha(staged) != digest:
                raise ValueError(f"Downloaded demo video checksum mismatch: {name}")
            staged.rename(target)
        print(f"Downloaded pinned demo video: {target}")


MISSION_DEMO_LINES = [
    "HANDHELD DEMONSTRATION MISSION PACKET",
    "Synthetic content. Not a real operation, location, unit or order.",
    "",
    "OBJECTIVE",
    "Exercise AMBER FIELD is a daylight site-security watch of two camera sectors.",
    "The operator reports unattended bags and any vehicle stopping in the car park.",
    "",
    "TIMES",
    "The watch runs from 09:00 to 09:30 local; the report is due at 09:35.",
    "",
    "LOCATIONS",
    "Sector 1 (camera 1): the car park, viewed from above.",
    "Sector 2 (camera 2): the tree-lined park avenue with benches.",
    "",
    "PEOPLE AND UNITS",
    "One handheld operator. The range warden on duty is the safety officer.",
    "",
    "RESTRICTIONS",
    "Observe and report only; do not approach or stop anyone.",
    "Do not record identities of members of the public in reports.",
    "",
    "RESOURCES",
    "One handheld device and two fixed cameras, one per sector.",
    "",
    "DEPENDENCIES",
    "The watch starts only when both cameras report a live feed.",
    "",
    "RISKS",
    "Crowds on the park avenue can hide people; moving vehicles in the car park.",
]


def _pdf_bytes(lines):
    """A minimal one-page PDF, so the demo packet needs no document toolchain."""
    def escape(text):
        return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    content = ["BT", "/F1 11 Tf", "14 TL", "54 760 Td"]
    for line in lines:
        content.append(f"({escape(line)}) Tj T*")
    content.append("ET")
    stream = "\n".join(content).encode("ascii", "replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    start = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{start}\n%%EOF\n").encode()
    return bytes(out)


MISSION_EXAMPLE = REPO / "examples/demo-mission-packet.pdf"


def write_mission(target=MISSION_EXAMPLE):
    """Regenerate the committed example packet after editing MISSION_DEMO_LINES."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_pdf_bytes(MISSION_DEMO_LINES))
    print(f"Wrote synthetic demo mission packet: {target}")


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


def chat_cache_ready(directory, models):
    """True when every model has its OpenVINO export and a resolvable Hub snapshot."""
    for model in models:
        if not (directory / model / "openvino_model.xml").is_file():
            return False
        repo = directory / ("models--" + model.replace("/", "--"))
        ref = repo / "refs/main"
        if not ref.is_file() or not (repo / "snapshots" / ref.read_text().strip()).is_dir():
            return False
    return True


def ir_problem(xml):
    """None when an OpenVINO IR is complete: its .bin holds every weight the .xml references."""
    xml = Path(xml)
    weights = xml.with_suffix(".bin")
    if not xml.is_file() or not weights.is_file():
        return "not downloaded"
    end = 0
    try:
        for _, element in ET.iterparse(xml):  # nosec B314 - local model IR
            if element.tag == "data" and "offset" in element.attrib and "size" in element.attrib:
                end = max(end, int(element.get("offset")) + int(element.get("size")))
            element.clear()
    except ET.ParseError as error:
        return f"{xml.name} is damaged ({error})"
    size = weights.stat().st_size
    if size < end:
        return f"{weights.name} is incomplete ({size} of {end} bytes)"
    return None


# Exit code of check when only demo-up assets are missing; make up can start.
DEMO_ONLY_MISSING = 3


def report(results, optional=()):
    """Print each result; optional ones (needed only by make demo-up) never fail."""
    for marker, items in (("MISSING", results), ("--", optional)):
        for label, problem, fix in items:
            print(f"ok       {label}" if problem is None else f"{marker:8} {label}: {problem}  -> {fix}", flush=True)
    if any(problem is not None for _, problem, _ in results):
        return 1
    return DEMO_ONLY_MISSING if any(problem is not None for _, problem, _ in optional) else 0


def chat_models():
    text = (REPO / "config/chatqna/config.yaml").read_text()
    return re.findall(r'^\s*(?:LLM|EMBEDDING|RERANKER)_MODEL_ID:\s*"([^"]+)"', text, re.MULTILINE)


def check(root, clips):
    """Report, without downloading anything, whether host-side assets are ready."""
    results = []
    try:
        problem = None if verify_model(root / MODEL_DIR) else "not downloaded"
    except ValueError as error:
        problem = str(error)
    results.append((f"{MODEL} INT8 detector (approved hashes)", problem, "make prereq"))
    demo = []
    video = root / "data/dlsps/demo-video"
    for name, _, digest in VIDEOS:
        path = video / name
        problem = None if path.is_file() and sha(path) == digest else (
            "checksum mismatch" if path.is_file() else "not downloaded")
        demo.append((f"demo video {name}", problem, "make demo-prereq (only make demo-up needs it)"))
    for clip in clips:
        source, fps = clip.rsplit(":", 1)
        name = f"{Path(source).stem}.max{fps}.ts"
        demo.append((f"demo camera clip {name}", None if (video / name).is_file() else "not prepared",
                     "make demo-prereq (only make demo-up needs it)"))
    cache = root / "data/chatqna/model_cache"
    for model in chat_models():
        problem = (ir_problem(cache / model / "openvino_model.xml")
                   or (None if chat_cache_ready(cache, [model]) else "Hub snapshot missing"))
        results.append((f"Chat Q&A model {model}", problem, "make up or make demo-up (internet)"))
    return report(results, demo)


def check_ir(base, files, fix):
    return report([(name, ir_problem(Path(base) / name), fix) for name in files])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check", "check-ir", "check-model", "model", "video", "mission"])
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("ASSET_ROOT", REPO)))
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--clips", nargs="*", default=[], help="<video>:<max fps> camera clips (check)")
    parser.add_argument("--fix", default="", help="command shown for missing items (check-ir)")
    parser.add_argument("files", nargs="*", help="OpenVINO .xml files relative to --root (check-ir)")
    args = parser.parse_intermixed_args()
    # Read-only checks take no lock: they must not wait behind a running download,
    # and check-ir also runs on read-only Docker volume mounts.
    if args.action == "check-ir":
        return check_ir(args.root, args.files, args.fix)
    if args.action == "check":
        return check(args.root, args.clips)
    args.root.mkdir(parents=True, exist_ok=True)
    with (args.root / ".demo-assets.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.action == "check-model":
            return 0 if verify_model(args.root / MODEL_DIR) else 2
        if args.action == "model":
            download_model(args.root, args.timeout)
        elif args.action == "mission":
            write_mission()
        else:
            download_video(args.root)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, RuntimeError, ET.ParseError) as error:
        sys.exit(f"demo-assets: {error}")

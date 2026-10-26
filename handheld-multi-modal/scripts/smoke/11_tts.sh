#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${TTS_URL:-}" ]]; then
  echo "BLOCKED: set TTS_URL (e.g. http://127.0.0.1:8012). This smoke checks" >&2
  echo "/health, /v1/model-info (asserts device=\$TTS_DEVICE, default CPU), and posts a real" >&2
  echo "sentence to /v1/audio/speech, asserting a non-empty valid WAV" >&2
  echo "response. Scope: microsoft/speecht5_tts/OpenVINO on GPU or CPU." >&2
  exit 2
fi
python="${SMOKE_PYTHON:-python3}"
command -v "$python" >/dev/null || {
  echo "BLOCKED: missing $python; this smoke installs nothing." >&2
  exit 2
}

expected="${TTS_DEVICE:-CPU}"
exec "$python" - "$TTS_URL" "${expected^^}" <<'PY'
import json
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request

base = sys.argv[1].rstrip("/")
expected = sys.argv[2]
parsed = urllib.parse.urlsplit(base)
if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
    print("BLOCKED: TTS_URL must be a plain HTTP(S) base without credentials.", file=sys.stderr)
    sys.exit(2)

try:
    with urllib.request.urlopen(f"{base}/health", timeout=10) as resp:
        health = json.loads(resp.read())
except (urllib.error.URLError, TimeoutError) as exc:
    print(f"BLOCKED: /health unreachable: {exc}", file=sys.stderr)
    sys.exit(2)

if health.get("status") != "ok":
    print(f"FAIL: /health returned {health!r}, expected status=ok", file=sys.stderr)
    sys.exit(1)

try:
    with urllib.request.urlopen(f"{base}/v1/model-info", timeout=10) as resp:
        info = json.loads(resp.read())
except (urllib.error.URLError, TimeoutError) as exc:
    print(f"FAIL: /v1/model-info unreachable: {exc}", file=sys.stderr)
    sys.exit(1)

if info.get("device") != expected:
    print(f"FAIL: /v1/model-info reports device={info.get('device')!r}, expected {expected}", file=sys.stderr)
    sys.exit(1)

body = json.dumps({"input": "The camera stream is ready.", "response_format": "wav"}).encode()
req = urllib.request.Request(
    f"{base}/v1/audio/speech",
    data=body,
    method="POST",
    headers={"Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(req, timeout=60) as resp:
        content_type = resp.headers.get("Content-Type", "")
        audio = resp.read()
except (urllib.error.URLError, TimeoutError) as exc:
    print(f"FAIL: speech synthesis request failed: {exc}", file=sys.stderr)
    sys.exit(1)

if "audio/wav" not in content_type:
    print(f"FAIL: unexpected Content-Type {content_type!r}", file=sys.stderr)
    sys.exit(1)

if len(audio) < 1000 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
    print(f"FAIL: response is not a valid non-trivial WAV file ({len(audio)} bytes)", file=sys.stderr)
    sys.exit(1)

print(f"PASS: /health ok; device={expected} confirmed via /v1/model-info; real "
      f"microsoft/speecht5_tts synthesis returned a valid {len(audio)}-byte WAV file.")
PY

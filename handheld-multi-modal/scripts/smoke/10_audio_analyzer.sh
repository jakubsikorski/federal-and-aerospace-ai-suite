#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${AUDIO_ANALYZER_URL:-}" || -z "${AUDIO_ANALYZER_SAMPLE_WAV:-}" ]]; then
  echo "BLOCKED: set AUDIO_ANALYZER_URL (e.g. http://127.0.0.1:8011) and" >&2
  echo "AUDIO_ANALYZER_SAMPLE_WAV to a real speech WAV file. This smoke posts" >&2
  echo "the file to /v1/audio/transcriptions (model=whisper-1 alias) and" >&2
  echo "checks /health. Scope: whisper-medium/OpenVINO/GPU only; no" >&2
  echo "diarization/sentiment pass (both are pinned disabled)." >&2
  exit 2
fi
python="${SMOKE_PYTHON:-python3}"
command -v "$python" >/dev/null || {
  echo "BLOCKED: missing $python; this smoke installs nothing." >&2
  exit 2
}
[[ -f "${AUDIO_ANALYZER_SAMPLE_WAV}" ]] || {
  echo "BLOCKED: AUDIO_ANALYZER_SAMPLE_WAV not found: ${AUDIO_ANALYZER_SAMPLE_WAV}" >&2
  exit 2
}

exec "$python" - "$AUDIO_ANALYZER_URL" "$AUDIO_ANALYZER_SAMPLE_WAV" <<'PY'
import json
import mimetypes
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

base, wav_path = sys.argv[1].rstrip("/"), sys.argv[2]
parsed = urllib.parse.urlsplit(base)
if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
    print("BLOCKED: AUDIO_ANALYZER_URL must be a plain HTTP(S) base without credentials.", file=sys.stderr)
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

with open(wav_path, "rb") as f:
    audio_bytes = f.read()

boundary = uuid.uuid4().hex
mime = mimetypes.guess_type(wav_path)[0] or "audio/wav"

def field(name, value):
    return (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
            f"{value}\r\n").encode()

body = b"".join([
    field("model", "whisper-1"),
    f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"sample.wav\"\r\n"
    f"Content-Type: {mime}\r\n\r\n".encode(),
    audio_bytes,
    b"\r\n",
    f"--{boundary}--\r\n".encode(),
])

req = urllib.request.Request(
    f"{base}/v1/audio/transcriptions",
    data=body,
    method="POST",
    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
)
try:
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read())
except (urllib.error.URLError, TimeoutError) as exc:
    print(f"FAIL: transcription request failed: {exc}", file=sys.stderr)
    sys.exit(1)

text = result.get("text", "")
if not text.strip():
    print(f"FAIL: empty transcription text, full response: {result!r}", file=sys.stderr)
    sys.exit(1)

print("PASS: /health ok; real whisper-medium/GPU transcription returned non-empty text:")
print(text)
PY

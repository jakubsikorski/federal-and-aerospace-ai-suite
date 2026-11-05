#!/usr/bin/env bash
# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Convert any video file (mp4, mov, mkv, avi, ts, webm, ... anything ffmpeg can
# read) into the .avi format the uav-vision-analytics pipelines expect.
#
# The pipelines read the sample videos with
#   multifilesrc location=<file>.avi loop=true ! h264parse ! decodebin3 ...
# and the output looks best / loops cleanly when the file is:
#   - H.264 (High profile, level 4.0, yuv420p), progressive, no audio
#   - AVI container
#   - constant frame rate (default 30 fps)
#   - a keyframe every GOP frames (default 250), closed GOPs, no scene-cut keyframes
#   - a whole number of GOPs long, so the loop point lands on a keyframe
#   - ~4 Mbps (the RTSP output encoder is capped at about 4 Mbps by default, so a
#     much higher source bitrate is mostly wasted)
#
# Usage:
#   ./scripts/convert_video.sh <input> [options]
#
# Options:
#   -o, --output <file.avi>   Output file (default: <input name>.avi next to the input;
#                             <input name>_converted.avi if the input is already .avi)
#   -b, --bitrate <rate>      Target video bitrate, e.g. 4M (default: 4M)
#   -r, --fps <n>             Output frame rate (default: 30)
#   -s, --size <WxH>          Scale (and letterbox) to WxH, e.g. 1920x1080 (default: keep source size)
#   -g, --gop <n>             Keyframe interval in frames (default: 250)
#   -n, --frames <n>          Output exactly this many frames. Use the same value for every
#                             camera so all loops have the same length and stay in step.
#   -p, --preset <name>       x264 preset (default: medium)
#       --no-trim             Keep every frame instead of trimming to whole GOPs
#   -f, --force               Overwrite the output file if it exists
#   -h, --help                Show this help
#
# ffmpeg is taken from the host if installed, otherwise from the DL Streamer
# Pipeline Server image already used by this app (DLSTREAMER_PIPELINE_SERVER_IMAGE
# in .env), so only Docker is required.
#
# Examples:
#   ./scripts/convert_video.sh my_drone_clip.mp4
#   ./scripts/convert_video.sh front.mp4 -o resources/videos/uav_sample_front.avi -n 1750 -f
#   ./scripts/convert_video.sh back.mov  -o resources/videos/uav_sample_back.avi  -n 1750 -f

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"

log()  { echo "[INFO ] $*"; }
warn() { echo "[WARN ] $*" >&2; }
err()  { echo "[ERROR] $*" >&2; }
usage() { sed -n '/^# Convert any/,/^set -Eeuo/p' "${BASH_SOURCE[0]}" | sed -e '/^set -Eeuo/d' -e 's/^# \{0,1\}//'; }

INPUT=""
OUTPUT=""
BITRATE="4M"
FPS="30"
SIZE=""
GOP="250"
FRAMES=""
PRESET="medium"
TRIM="true"
FORCE="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -o|--output)  OUTPUT="${2:?missing value for $1}"; shift 2 ;;
    -b|--bitrate) BITRATE="${2:?missing value for $1}"; shift 2 ;;
    -r|--fps)     FPS="${2:?missing value for $1}"; shift 2 ;;
    -s|--size)    SIZE="${2:?missing value for $1}"; shift 2 ;;
    -g|--gop)     GOP="${2:?missing value for $1}"; shift 2 ;;
    -n|--frames)  FRAMES="${2:?missing value for $1}"; shift 2 ;;
    -p|--preset)  PRESET="${2:?missing value for $1}"; shift 2 ;;
    --no-trim)    TRIM="false"; shift ;;
    -f|--force)   FORCE="true"; shift ;;
    -h|--help)    usage; exit 0 ;;
    -*)           err "Unknown option: $1"; usage; exit 1 ;;
    *)            if [[ -z "${INPUT}" ]]; then INPUT="$1"; else err "Unexpected argument: $1"; exit 1; fi; shift ;;
  esac
done

[[ -n "${INPUT}" ]] || { err "No input file given."; usage; exit 1; }
[[ -f "${INPUT}" ]] || { err "Input file not found: ${INPUT}"; exit 1; }
[[ "${FPS}"  =~ ^[0-9]+$ && "${FPS}"  -gt 0 ]] || { err "--fps must be a positive integer"; exit 1; }
[[ "${GOP}"  =~ ^[0-9]+$ && "${GOP}"  -gt 0 ]] || { err "--gop must be a positive integer"; exit 1; }
[[ -z "${FRAMES}" || ( "${FRAMES}" =~ ^[0-9]+$ && "${FRAMES}" -gt 0 ) ]] || { err "--frames must be a positive integer"; exit 1; }
[[ -z "${SIZE}" || "${SIZE}" =~ ^[0-9]+x[0-9]+$ ]] || { err "--size must look like 1920x1080"; exit 1; }
[[ "${BITRATE}" =~ ^[0-9]+(\.[0-9]+)?[kKmM]?$ ]] || { err "--bitrate must look like 4M or 4000k"; exit 1; }

INPUT="$(readlink -f "${INPUT}")"
if [[ -z "${OUTPUT}" ]]; then
  OUTPUT="${INPUT%.*}.avi"
  # An .avi input would map onto itself; write <name>_converted.avi instead.
  [[ "${OUTPUT}" != "${INPUT}" ]] || OUTPUT="${INPUT%.*}_converted.avi"
fi
[[ "${OUTPUT}" == *.avi ]] || { err "Output must end in .avi (got: ${OUTPUT})"; exit 1; }
mkdir -p "$(dirname "${OUTPUT}")"
OUTPUT="$(readlink -f "$(dirname "${OUTPUT}")")/$(basename "${OUTPUT}")"
[[ "${INPUT}" != "${OUTPUT}" ]] || { err "Input and output are the same file."; exit 1; }
if [[ -e "${OUTPUT}" && "${FORCE}" != "true" ]]; then
  err "Output exists: ${OUTPUT} (use --force to overwrite)"
  exit 1
fi

# ── Pick ffmpeg: host first, then the app's DL Streamer image ────────────────
IN_DIR="$(dirname "${INPUT}")";   IN_NAME="$(basename "${INPUT}")"
OUT_DIR="$(dirname "${OUTPUT}")"; OUT_NAME="$(basename "${OUTPUT}")"

if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
  MODE="host"
  IN_PATH="${INPUT}"
  OUT_PATH="${OUTPUT}"
  ffmpeg_run()  { ffmpeg  "$@"; }
  ffprobe_run() { ffprobe "$@"; }
else
  command -v docker >/dev/null 2>&1 || { err "Neither ffmpeg nor docker is installed. Install one of them."; exit 1; }
  IMAGE="${DLSTREAMER_PIPELINE_SERVER_IMAGE:-}"
  if [[ -z "${IMAGE}" && -f "${ENV_FILE}" ]]; then
    IMAGE="$(grep -E '^DLSTREAMER_PIPELINE_SERVER_IMAGE=' "${ENV_FILE}" | head -1 | cut -d= -f2-)"
  fi
  if [[ -z "${IMAGE}" ]]; then
    IMAGE="$(docker images --format '{{.Repository}}:{{.Tag}}' | grep -m1 'dlstreamer-pipeline-server' || true)"
  fi
  [[ -n "${IMAGE}" ]] || { err "ffmpeg not found and no DL Streamer image available. Install ffmpeg (sudo apt-get install -y ffmpeg)."; exit 1; }
  MODE="docker (${IMAGE})"
  IN_PATH="/in/${IN_NAME}"
  OUT_PATH="/out/${OUT_NAME}"
  docker_ff() {
    local bin="$1"; shift
    docker run --rm --entrypoint "${bin}" --user "$(id -u):$(id -g)" \
      -v "${IN_DIR}:/in:ro" -v "${OUT_DIR}:/out" "${IMAGE}" "$@"
  }
  ffmpeg_run()  { docker_ff ffmpeg  "$@"; }
  ffprobe_run() { docker_ff ffprobe "$@"; }
fi
log "Using ffmpeg from: ${MODE}"

# ── Probe the input ───────────────────────────────────────────────────────────
probe() { ffprobe_run -v error -select_streams v:0 -show_entries "$1" -of default=nw=1:nk=1 "$2" 2>/dev/null | head -1 || true; }

SRC_CODEC="$(probe stream=codec_name "${IN_PATH}")"
SRC_W="$(probe stream=width "${IN_PATH}")"
SRC_H="$(probe stream=height "${IN_PATH}")"
SRC_DUR="$(probe format=duration "${IN_PATH}")"
[[ -n "${SRC_CODEC}" && -n "${SRC_W}" ]] || { err "Could not read a video stream from ${INPUT}"; exit 1; }
if [[ -z "${SRC_DUR}" || "${SRC_DUR}" == "N/A" ]]; then
  SRC_DUR="$(ffprobe_run -v error -select_streams v:0 -show_entries stream=duration -of default=nw=1:nk=1 "${IN_PATH}" 2>/dev/null | head -1 || true)"
fi
[[ -n "${SRC_DUR}" && "${SRC_DUR}" != "N/A" ]] || { err "Could not determine the duration of ${INPUT}"; exit 1; }
log "Input : ${IN_NAME} (${SRC_CODEC}, ${SRC_W}x${SRC_H}, ${SRC_DUR}s)"

# ── Work out how many frames to write ─────────────────────────────────────────
TOTAL_FRAMES="$(awk -v d="${SRC_DUR}" -v f="${FPS}" 'BEGIN{printf "%d", d*f}')"
[[ "${TOTAL_FRAMES}" -gt 0 ]] || { err "Input is too short."; exit 1; }

if [[ -n "${FRAMES}" ]]; then
  OUT_FRAMES="${FRAMES}"
  if [[ "${OUT_FRAMES}" -gt "${TOTAL_FRAMES}" ]]; then
    err "--frames ${FRAMES} is more than the input provides at ${FPS} fps (${TOTAL_FRAMES})."
    exit 1
  fi
elif [[ "${TRIM}" == "true" ]]; then
  if [[ "${TOTAL_FRAMES}" -lt "${GOP}" ]]; then
    GOP="${TOTAL_FRAMES}"
    warn "Input is shorter than one GOP; using a GOP of ${GOP} frames."
  fi
  OUT_FRAMES=$(( TOTAL_FRAMES / GOP * GOP ))
else
  OUT_FRAMES="${TOTAL_FRAMES}"
fi
OUT_SECS="$(awk -v n="${OUT_FRAMES}" -v f="${FPS}" 'BEGIN{printf "%.2f", n/f}')"
log "Output: ${OUT_NAME} -> ${OUT_FRAMES} frames @ ${FPS} fps (${OUT_SECS}s), GOP ${GOP}, ${BITRATE}"
if [[ -z "${FRAMES}" ]]; then
  log "Tip: for several cameras use the same --frames N for every file so their loops stay in step."
fi

# ── Encode ────────────────────────────────────────────────────────────────────
VF="fps=${FPS}"
if [[ -n "${SIZE}" ]]; then
  W="${SIZE%x*}"; H="${SIZE#*x}"
  VF="${VF},scale=${W}:${H}:force_original_aspect_ratio=decrease,pad=${W}:${H}:(ow-iw)/2:(oh-ih)/2"
fi
VF="${VF},format=yuv420p"

# maxrate/bufsize keep the bitrate close to the target (VBV-constrained)
RATE_NUM="${BITRATE//[!0-9.]/}"; RATE_UNIT="${BITRATE//[0-9.]/}"
MAXRATE="$(awk -v r="${RATE_NUM}" 'BEGIN{printf "%.2f", r*1.1}')${RATE_UNIT}"
BUFSIZE="$(awk -v r="${RATE_NUM}" 'BEGIN{printf "%.2f", r*2}')${RATE_UNIT}"

TMP_NAME=".${OUT_NAME}.tmp.$$.avi"
cleanup() { rm -f "${OUT_DIR}/${TMP_NAME}"; }
trap cleanup EXIT

log "Encoding (this can take a while for long or high-resolution clips)..."
ffmpeg_run -v error -stats -y -i "${IN_PATH}" \
  -map 0:v:0 -an -sn -dn \
  -vf "${VF}" -r "${FPS}" -frames:v "${OUT_FRAMES}" \
  -c:v libx264 -preset "${PRESET}" -profile:v high -level 4.0 \
  -b:v "${BITRATE}" -maxrate "${MAXRATE}" -bufsize "${BUFSIZE}" \
  -bf 2 -g "${GOP}" -keyint_min "${GOP}" \
  -x264-params "scenecut=0:open-gop=0" \
  -f avi "${OUT_PATH%/*}/${TMP_NAME}"

mv -f "${OUT_DIR}/${TMP_NAME}" "${OUTPUT}"
trap - EXIT

# ── Verify the result ─────────────────────────────────────────────────────────
VERIFY="${OUT_PATH}"
V_CODEC="$(probe stream=codec_name "${VERIFY}")"
V_PROFILE="$(probe stream=profile "${VERIFY}")"
V_PIX="$(probe stream=pix_fmt "${VERIFY}")"
V_W="$(probe stream=width "${VERIFY}")"
V_H="$(probe stream=height "${VERIFY}")"
V_FPS="$(probe stream=r_frame_rate "${VERIFY}")"
V_BR="$(probe stream=bit_rate "${VERIFY}")"
V_FRAMES="$(ffprobe_run -v error -count_frames -select_streams v:0 -show_entries stream=nb_read_frames -of default=nw=1:nk=1 "${VERIFY}" | head -1)"
KEYS="$(ffprobe_run -v error -select_streams v:0 -show_entries packet=flags -of csv=p=0 "${VERIFY}" | awk '/K/{printf "%d ", NR-1}')"
DEC_ERRS="$(ffmpeg_run -v error -i "${VERIFY}" -f null - 2>&1 | grep -c . || true)"

FIRST_KEY="${KEYS%% *}"
BAD_KEYS="$(echo "${KEYS}" | awk -v g="${GOP}" '{for(i=1;i<=NF;i++) if ($i % g != 0) c++} END{print c+0}')"

echo
log "Result: ${OUTPUT}"
echo "        codec=${V_CODEC} profile=${V_PROFILE} pix_fmt=${V_PIX} ${V_W}x${V_H} @ ${V_FPS} fps"
echo "        frames=${V_FRAMES} bitrate=$(awk -v b="${V_BR:-0}" 'BEGIN{printf "%.2f", b/1000000}') Mbps size=$(du -h "${OUTPUT}" | cut -f1)"
echo "        keyframes at: ${KEYS}"

OK="true"
[[ "${V_CODEC}" == "h264" ]]                       || { err "Output codec is ${V_CODEC}, expected h264"; OK="false"; }
[[ "${V_PIX}" == "yuv420p" ]]                      || { err "Output pixel format is ${V_PIX}, expected yuv420p"; OK="false"; }
[[ "${V_FRAMES}" == "${OUT_FRAMES}" ]]             || { err "Output has ${V_FRAMES} frames, expected ${OUT_FRAMES}"; OK="false"; }
[[ "${FIRST_KEY}" == "0" ]]                        || { err "Output does not start on a keyframe"; OK="false"; }
[[ "${BAD_KEYS}" == "0" ]]                         || { warn "Some keyframes are not on a ${GOP}-frame boundary"; }
[[ "${DEC_ERRS}" == "0" ]]                         || { err "Decoding reported ${DEC_ERRS} error line(s)"; OK="false"; }

if [[ "${OK}" != "true" ]]; then
  err "Verification failed. The file was written but may not work correctly in the pipelines."
  exit 1
fi

log "Verified: valid H.264/AVI, starts on a keyframe, decodes without errors."
echo
echo "Next: put the file in resources/videos/ (the pipelines read uav_sample_front.avi,"
echo "uav_sample_back.avi, uav_sample_bottom.avi for CPU/GPU/NPU), then restart the pipelines:"
echo "  make start-rtsp"

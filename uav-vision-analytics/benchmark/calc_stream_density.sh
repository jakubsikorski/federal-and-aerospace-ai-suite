#!/bin/bash
# ==============================================================================
# UAV Vision Analytics - Stream Density Benchmark
#
# Finds how many concurrent camera streams one pipeline sustains at a target FPS
# and records CPU / GPU / NPU utilisation, frequencies, power and memory while
# doing so.
#
# The scenario (detection device, captioning on/off, captioning device) comes
# from configs/cameras-pymavlink.yaml and can be overridden with flags, so every
# combination of detection (cpu|gpu|npu) and captioning (off | cpu | gpu) runs
# without editing any file. All requests go through nginx (https://<HOST_IP>).
#
#   ./calc_stream_density.sh                       # use the YAML as is
#   ./calc_stream_density.sh -d CPU -c false       # detection only
#   ./calc_stream_density.sh -d NPU -c true -cd GPU
#   ./calc_stream_density.sh -n 6                  # one fixed run with 6 streams
#
# All streams share one detection model instance, exactly as the pipeline
# manager starts the cameras of the application.
# ==============================================================================

set -o pipefail

# Local nginx requests must never go through a proxy.
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
export no_proxy="*" NO_PROXY="*"
# ~/.local/bin may hold a jq wrapper.
export PATH="$HOME/.local/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG_FILE="${CAMERAS_CONFIG:-${APP_ROOT}/configs/cameras-pymavlink.yaml}"
ENV_FILE="${APP_ROOT}/.env"
RESULTS_ROOT="${RESULTS_DIR:-${SCRIPT_DIR}/results}"
WORK_DIR="$(mktemp -d /tmp/uav_bench.XXXXXX)"

MODELS_DIR="/home/pipeline-server/resources/models"
VLM_DIR="/home/pipeline-server/resources/ov_models"
BBOX_THICKNESS="${BBOX_THICKNESS:-6}"
NGINX_CONTAINER="${NGINX_CONTAINER:-uav-va-nginx}"
METRICS_CONTAINER="${METRICS_CONTAINER:-uav-va-metrics-manager}"

for tool in curl jq gawk python3; do
    command -v "$tool" >/dev/null 2>&1 || { echo "[ERROR] '$tool' is required but not installed."; exit 1; }
done

# ------------------------------------------------------------------------------
# Defaults (every one can be changed with a flag)
# ------------------------------------------------------------------------------
TARGET_FPS=28.5
MIN_STREAMS=1
MAX_STREAMS=64
WINDOW_SECONDS=60
WARMUP_SECONDS=10
SAMPLE_INTERVAL=2
FIXED_STREAMS=""

CLI_DET_DEVICE=""
CLI_CAPTION_ENABLED=""
CLI_CAP_DEVICE=""
CLI_CAP_TOKENS=""
CLI_PIPELINE_NAME=""
CLI_BASE_URL=""

HW_MONITOR_ENABLED=true
METRICS_INTERVAL="${METRICS_INTERVAL:-2}"
METRICS_URL="${METRICS_URL:-}"

usage() {
    cat <<EOF
Usage: $0 [OPTIONS]

Scenario (default: values of ${CONFIG_FILE#${APP_ROOT}/}):
  -d,  --det-device <CPU|GPU|NPU>   Detection device
  -c,  --captioning <true|false>    Enable / disable VLM captioning
  -cd, --cap-device <CPU|GPU>       Captioning device (the VLM has no NPU support)
  -ct, --cap-tokens <num>           Caption length limit (max_new_tokens, default: from YAML)
  -p,  --pipeline <name>            Use this pipeline name instead of deriving it

Search:
  -t,  --target-fps <fps>           Minimum FPS every stream must sustain (default: ${TARGET_FPS})
  -s,  --min-streams <num>          Start of the search (default: ${MIN_STREAMS})
  -m,  --max-streams <num>          Upper limit of the search (default: ${MAX_STREAMS})
  -n,  --streams <num>              Run exactly this many streams once (no search)
  -w,  --window <sec>               Measurement window per run (default: ${WINDOW_SECONDS})
       --warmup <sec>               Wait after all streams run, before measuring (default: ${WARMUP_SECONDS})

Connection / metrics:
  -u,  --url <base_url>             DL Streamer Pipeline Server URL behind nginx (auto-detected)
       --metrics-url <url>          metrics-manager URL behind nginx (default: same host)
       --metrics-interval <sec>     Hardware polling interval (default: ${METRICS_INTERVAL})
       --no-hw-metrics              Skip CPU / GPU / NPU / power collection

  -l,  --list-pipelines             List the pipelines registered in the server and exit
  -h,  --help                       Show this help

Results are written to ${RESULTS_ROOT#${APP_ROOT}/}/<timestamp>_<pipeline>/.
EOF
    exit "${1:-1}"
}

need_value() {
    case "$1" in
        -l|--list-pipelines|-h|--help|--no-hw-metrics) return 0 ;;
    esac
    [ "$2" -ge 2 ] || { echo "[ERROR] Option $1 needs a value."; usage; }
}

while [[ "$#" -gt 0 ]]; do
    need_value "$1" "$#"
    case "$1" in
        -d|--det-device)        CLI_DET_DEVICE=$(tr '[:lower:]' '[:upper:]' <<< "$2"); shift 2 ;;
        -c|--captioning)        CLI_CAPTION_ENABLED=$(tr '[:upper:]' '[:lower:]' <<< "$2"); shift 2 ;;
        -cd|--cap-device)       CLI_CAP_DEVICE=$(tr '[:lower:]' '[:upper:]' <<< "$2"); shift 2 ;;
        -ct|--cap-tokens)       CLI_CAP_TOKENS="$2"; shift 2 ;;
        -p|--pipeline)          CLI_PIPELINE_NAME="$2"; shift 2 ;;
        -u|--url)               CLI_BASE_URL="${2%/}"; shift 2 ;;
        -t|--target-fps)        TARGET_FPS="$2"; shift 2 ;;
        -s|--min-streams)       MIN_STREAMS="$2"; shift 2 ;;
        -m|--max-streams)       MAX_STREAMS="$2"; shift 2 ;;
        -n|--streams)           FIXED_STREAMS="$2"; shift 2 ;;
        -w|--window)            WINDOW_SECONDS="$2"; shift 2 ;;
        --warmup)               WARMUP_SECONDS="$2"; shift 2 ;;
        --metrics-url)          METRICS_URL="${2%/}"; shift 2 ;;
        --metrics-interval)     METRICS_INTERVAL="$2"; shift 2 ;;
        --no-hw-metrics)        HW_MONITOR_ENABLED=false; shift ;;
        -l|--list-pipelines)    LIST_ONLY=true; shift ;;
        -h|--help)              usage 0 ;;
        *) echo "[ERROR] Unknown argument: $1"; usage ;;
    esac
done

is_uint() { [[ "$1" =~ ^[0-9]+$ ]]; }
is_num()  { [[ "$1" =~ ^[0-9]+(\.[0-9]+)?$ ]]; }
for pair in "min-streams:$MIN_STREAMS" "max-streams:$MAX_STREAMS" "window:$WINDOW_SECONDS" "warmup:$WARMUP_SECONDS"; do
    is_uint "${pair#*:}" || { echo "[ERROR] --${pair%%:*} must be a whole number."; exit 1; }
done
is_num "$TARGET_FPS" || { echo "[ERROR] --target-fps must be a number."; exit 1; }
is_num "$METRICS_INTERVAL" || { echo "[ERROR] --metrics-interval must be a number."; exit 1; }
if [ -n "$FIXED_STREAMS" ]; then
    { is_uint "$FIXED_STREAMS" && [ "$FIXED_STREAMS" -ge 1 ]; } || { echo "[ERROR] --streams must be a whole number >= 1."; exit 1; }
fi
if [ "$MIN_STREAMS" -lt 1 ] || [ "$MIN_STREAMS" -gt "$MAX_STREAMS" ]; then
    echo "[ERROR] Need 1 <= --min-streams ($MIN_STREAMS) <= --max-streams ($MAX_STREAMS)."; exit 1
fi

# ------------------------------------------------------------------------------
# nginx endpoint detection
# ------------------------------------------------------------------------------
detect_nginx_host() {
    if [ -n "$DLSPS_NODE_IP" ]; then echo "$DLSPS_NODE_IP"; return; fi

    local bind env_ip
    bind=$(docker port "${NGINX_CONTAINER}" 443/tcp 2>/dev/null | head -n 1 | cut -d: -f1)
    if [ -n "$bind" ] && [ "$bind" != "0.0.0.0" ] && [ "$bind" != "::" ]; then echo "$bind"; return; fi

    if [ -f "$ENV_FILE" ]; then
        env_ip=$(grep -E "^HOST_IP=" "$ENV_FILE" | head -n 1 | cut -d= -f2- | tr -d "\"' \r")
        if [ -n "$env_ip" ]; then echo "$env_ip"; return; fi
    fi
    hostname -I | awk '{print $1}'
}

discover_base_url() {
    local host_ip="$1" candidate code
    for candidate in "https://${host_ip}" "https://${host_ip}/api"; do
        code=$(curl -k -s --connect-timeout 5 -o /dev/null -w "%{http_code}" "${candidate}/pipelines/status" 2>/dev/null)
        if [ "$code" == "200" ]; then echo "$candidate"; return 0; fi
    done
    echo "https://${host_ip}"
    return 1
}

if [ -n "$CLI_BASE_URL" ]; then
    BASE_URL="$CLI_BASE_URL"
else
    DLSPS_NODE_IP=$(detect_nginx_host)
    BASE_URL=$(discover_base_url "$DLSPS_NODE_IP")
fi
if [ -z "$METRICS_URL" ]; then
    if [[ "$BASE_URL" =~ ^(https?://[^/]+) ]]; then METRICS_URL="${BASH_REMATCH[1]}"; else METRICS_URL="$BASE_URL"; fi
fi

api()        { curl -k -s --connect-timeout 5 --max-time 30 "$@"; }
get_status() { api "${BASE_URL}/pipelines/status"; }

if [ "${LIST_ONLY:-false}" == "true" ]; then
    api "${BASE_URL}/pipelines" | jq -r '.[].version' | sort
    exit 0
fi

# ------------------------------------------------------------------------------
# Scenario: cameras-pymavlink.yaml + flag overrides -> pipeline name + payload inputs
# ------------------------------------------------------------------------------
# The first enabled camera that plays a video file defines the defaults.
read_yaml_defaults() {
    [ -f "$CONFIG_FILE" ] || return 0
    python3 - "$CONFIG_FILE" <<'PYEOF' 2>/dev/null
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1])) or {}
cams = [c for c in cfg.get("cameras", []) if c.get("enabled", True)]
files = [c for c in cams if c.get("video")]
if not cams:
    sys.exit(0)
ref = (files or cams)[0]
det, cap = ref.get("detection") or {}, ref.get("captioning") or {}
print("YAML_CAMERA=%s" % ref.get("id", ""))
print("YAML_DET_DEVICE=%s" % det.get("device", ""))
print("YAML_DET_MODEL=%s" % det.get("model", ""))
print("YAML_CAP_ENABLED=%s" % str(cap.get("enabled", False)).lower())
print("YAML_CAP_DEVICE=%s" % cap.get("device", ""))
print("YAML_CAP_MODEL=%s" % cap.get("model", ""))
print("YAML_CAP_PROMPT=%s" % cap.get("prompt", ""))
print("YAML_CAP_TOKENS=%s" % cap.get("max_new_tokens", ""))
print("YAML_VIDEOS=%s" % ",".join(c["video"] for c in files))
PYEOF
}

resolve_scenario() {
    local k v
    while IFS='=' read -r k v; do
        case "$k" in YAML_*) printf -v "$k" '%s' "$v" ;; esac
    done < <(read_yaml_defaults)

    if [ -z "$YAML_CAMERA" ]; then
        echo "[WARN] Could not read defaults from ${CONFIG_FILE}; using built-in defaults (use -d/-c/-cd)."
    fi

    DET_DEVICE=$(tr '[:lower:]' '[:upper:]' <<< "${CLI_DET_DEVICE:-${YAML_DET_DEVICE:-NPU}}")
    CAPTION_ENABLED="${CLI_CAPTION_ENABLED:-${YAML_CAP_ENABLED:-false}}"
    CAP_DEVICE=$(tr '[:lower:]' '[:upper:]' <<< "${CLI_CAP_DEVICE:-${YAML_CAP_DEVICE:-GPU}}")
    DET_MODEL="${YAML_DET_MODEL:-yolo11s/yolo11s_openvino_model/yolo11s.xml}"
    CAP_MODEL="${YAML_CAP_MODEL:-InternVL2-1B}"
    CAP_PROMPT="${YAML_CAP_PROMPT:-Describe what you see in the scene.}"
    CAP_TOKENS="${CLI_CAP_TOKENS:-${YAML_CAP_TOKENS:-50}}"

    [[ "$DET_DEVICE" =~ ^(CPU|GPU|NPU)$ ]] || { echo "[ERROR] Invalid detection device '$DET_DEVICE'. Use CPU, GPU or NPU."; exit 1; }
    [[ "$CAPTION_ENABLED" =~ ^(true|false)$ ]] || { echo "[ERROR] Invalid --captioning '$CAPTION_ENABLED'. Use true or false."; exit 1; }
    if [ "$CAPTION_ENABLED" == "true" ]; then
        [[ "$CAP_DEVICE" =~ ^(CPU|GPU)$ ]] || { echo "[ERROR] Invalid captioning device '$CAP_DEVICE'. Use CPU or GPU (the VLM does not run on NPU)."; exit 1; }
    fi
    # Same rules as the pipelines' parameter schemas; the values end up in the request.
    [[ "$DET_MODEL" =~ ^[A-Za-z0-9_./-]+\.xml$ && "$DET_MODEL" != *..* && "$DET_MODEL" != /* ]] || { echo "[ERROR] Invalid detection model '$DET_MODEL'."; exit 1; }
    [[ "$CAP_MODEL" =~ ^[A-Za-z0-9_.-]+$ ]] || { echo "[ERROR] Invalid captioning model '$CAP_MODEL'."; exit 1; }
    [[ "$CAP_PROMPT" =~ ^[A-Za-z0-9\ ,.?\'-]+$ ]] || { echo "[ERROR] Invalid captioning prompt in ${CONFIG_FILE}."; exit 1; }
    { is_uint "$CAP_TOKENS" && [ "$CAP_TOKENS" -ge 1 ] && [ "$CAP_TOKENS" -le 4096 ]; } || { echo "[ERROR] Invalid max_new_tokens '$CAP_TOKENS'."; exit 1; }

    local det_lower cap_lower
    det_lower=$(tr '[:upper:]' '[:lower:]' <<< "$DET_DEVICE")
    cap_lower=$(tr '[:upper:]' '[:lower:]' <<< "$CAP_DEVICE")
    VLM_PATH="${VLM_DIR}/${cap_lower}/${CAP_MODEL}"
    # Cameras with the same device, model and token limit share one loaded VLM, as in the application.
    VLM_INSTANCE_ID=$(tr '[:upper:]' '[:lower:]' <<< "instvlm_${cap_lower}_${CAP_MODEL}_t${CAP_TOKENS}" | tr -c 'a-z0-9_\n' '_')

    if [ -n "$CLI_PIPELINE_NAME" ]; then
        PIPELINE_NAME="$CLI_PIPELINE_NAME"
    elif [ "$CAPTION_ENABLED" == "true" ]; then
        PIPELINE_NAME="uav_object_detection_${det_lower}_${cap_lower}"
    else
        PIPELINE_NAME="uav_object_detection_${det_lower}"
    fi
    [[ "$PIPELINE_NAME" =~ ^[A-Za-z0-9_-]+$ ]] || { echo "[ERROR] Invalid pipeline name '$PIPELINE_NAME'."; exit 1; }

    # Cameras with the same device and model share one loaded model, as in the application.
    DET_INSTANCE_ID=$(tr '[:upper:]' '[:lower:]' <<< "inst${det_lower}_$(basename "${DET_MODEL%.xml}")" | tr -c 'a-z0-9_\n' '_')
    LABEL_TAG="${DET_DEVICE}"
    [ "$CAPTION_ENABLED" == "true" ] && LABEL_TAG="${DET_DEVICE}-${CAP_DEVICE}-Caption"

    IFS=',' read -ra VIDEOS <<< "${YAML_VIDEOS:-}"
    [ "${#VIDEOS[@]}" -gt 0 ] || VIDEOS=("uav_sample_front.avi" "uav_sample_bottom.avi" "uav_sample_back.avi")

    echo "======================================================"
    echo " UAV Benchmark Scenario"
    echo "   Endpoint (nginx)    : ${BASE_URL}"
    echo "   Defaults from       : ${CONFIG_FILE#${APP_ROOT}/}${YAML_CAMERA:+ (camera '${YAML_CAMERA}')}"
    echo "   Detection           : ${DET_DEVICE}  (${DET_MODEL})"
    if [ "$CAPTION_ENABLED" == "true" ]; then
        echo "   Captioning          : true  (${CAP_DEVICE}, ${CAP_MODEL}, ${CAP_TOKENS} tokens, one VLM instance shared by all streams)"
    else
        echo "   Captioning          : false"
    fi
    echo "   Pipeline            : ${PIPELINE_NAME}  (model instance ${DET_INSTANCE_ID}, shared by all streams)"
    if [ -n "$FIXED_STREAMS" ]; then
        echo "   Streams             : ${FIXED_STREAMS} (fixed)"
    else
        echo "   Streams searched    : ${MIN_STREAMS}..${MAX_STREAMS}"
    fi
    echo "   Target FPS/stream   : ${TARGET_FPS}   window: ${WINDOW_SECONDS}s   warm-up: ${WARMUP_SECONDS}s"
    echo "======================================================"
}

check_pipeline_exists() {
    local names
    names=$(api "${BASE_URL}/pipelines" | jq -r '.[].version' 2>/dev/null)
    if ! grep -qx "$PIPELINE_NAME" <<< "$names"; then
        echo "[ERROR] Pipeline '${PIPELINE_NAME}' is not registered in the pipeline server."
        echo "        Registered pipelines:"
        sed 's/^/          /' <<< "$names"
        exit 1
    fi
}

# ═══════════════════════════════════════════════════════════════════════════════
#  Hardware metrics (metrics-manager through nginx: SSE first, REST fallback;
#  RAPL package power read directly or through the metrics-manager container)
# ═══════════════════════════════════════════════════════════════════════════════
HW_POLL_PID=""
HW_RAPL_PID=""
HW_STREAM_PY="${WORK_DIR}/hw_stream.py"
HW_RAPL_PY="${WORK_DIR}/hw_rapl.py"
RAPL_ENERGY_PATH="/sys/class/powercap/intel-rapl/intel-rapl:0/energy_uj"

check_metrics_manager() {
    local resp
    resp=$(curl -k -s --connect-timeout 3 --max-time 4 -N "${METRICS_URL}/metrics/stream" 2>/dev/null | head -1)
    [ -n "$resp" ] && return 0
    resp=$(curl -k -s --connect-timeout 3 --max-time 5 "${METRICS_URL}/api/v1/metrics/latest" 2>/dev/null)
    grep -q '"metrics"' <<< "$resp"
}

write_hw_helpers() {
    cat > "$HW_STREAM_PY" << 'PYEOF'
import sys, json, ssl, urllib.request, time
outfile, base_url, interval = sys.argv[1], sys.argv[2].rstrip("/"), float(sys.argv[3])
sse_url, rest_url = base_url + "/metrics/stream", base_url + "/api/v1/metrics/latest"
_SSL_CTX = ssl._create_unverified_context()   # nginx uses a self-signed certificate

def _key(name, labels):
    tag = labels.get("type") or labels.get("engine") or labels.get("domain") or ""
    return (name + "__" + tag) if tag else name

def _write(mlist, f):
    for m in mlist:
        name, val = m.get("name", ""), m.get("value")
        if val is None or not name:
            continue
        labels = m.get("labels", {})
        if labels.get("gpu_id") not in (None, "0"):
            continue
        f.write(_key(name, labels) + "=" + str(val) + "\n")
    f.write("---\n")
    f.flush()

def _sse(f):
    req = urllib.request.urlopen(sse_url, context=_SSL_CTX)
    for raw in req:
        line = raw.decode("utf-8", errors="replace").strip()
        if line.startswith("data: "):
            try:
                ml = json.loads(line[6:]).get("metrics", [])
                if isinstance(ml, list):
                    _write(ml, f)
            except Exception:
                pass

def _rest(f):
    while True:
        try:
            with urllib.request.urlopen(rest_url, timeout=5, context=_SSL_CTX) as r:
                ml = []
                for k, e in json.loads(r.read()).get("metrics", {}).items():
                    v = e.get("fields", {}).get("value") if isinstance(e, dict) else None
                    if v is not None:
                        ml.append({"name": e.get("name", k.split("{")[0]), "labels": e.get("tags", {}), "value": v})
                _write(ml, f)
        except Exception:
            pass
        time.sleep(interval)

with open(outfile, "a") as f:
    try:
        _sse(f)
    except Exception:
        _rest(f)
PYEOF

    cat > "$HW_RAPL_PY" << 'PYEOF'
import sys, time, subprocess
outfile, energy_file, interval, container = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]

def _read(path):
    try:
        return int(open(path).read().strip())
    except PermissionError:
        pass            # energy_uj is root-only on recent kernels
    except OSError:
        return None
    try:
        r = subprocess.run(["docker", "exec", container, "cat", path],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return int(r.stdout.strip())
    except Exception:
        pass
    return None

max_uj = _read(energy_file.replace("energy_uj", "max_energy_range_uj")) or 262143000000
prev_uj = _read(energy_file)
if prev_uj is None:
    sys.exit(0)
prev_ts = time.monotonic()
while True:
    time.sleep(interval)
    curr_uj = _read(energy_file)
    if curr_uj is None:
        break
    curr_ts = time.monotonic()
    dt = curr_ts - prev_ts
    if dt > 0:
        delta = curr_uj - prev_uj if curr_uj >= prev_uj else max_uj - prev_uj + curr_uj
        with open(outfile, "a") as f:
            f.write("rapl_power_w__pkg={:.3f}\n".format(delta / 1e6 / dt))
    prev_uj, prev_ts = curr_uj, curr_ts
PYEOF
}

start_hw_monitor() {
    HW_SAMPLE_FILE="$1/hw_samples.log"
    rm -f "$HW_SAMPLE_FILE"
    $HW_MONITOR_ENABLED || return 0

    python3 -u "$HW_STREAM_PY" "$HW_SAMPLE_FILE" "$METRICS_URL" "$METRICS_INTERVAL" &
    HW_POLL_PID=$!
    if [ -e "$RAPL_ENERGY_PATH" ]; then
        python3 -u "$HW_RAPL_PY" "$HW_SAMPLE_FILE" "$RAPL_ENERGY_PATH" "$METRICS_INTERVAL" "$METRICS_CONTAINER" &
        HW_RAPL_PID=$!
    fi
}

stop_hw_monitor() {
    local pid
    for pid in "$HW_POLL_PID" "$HW_RAPL_PID"; do
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null
            wait "$pid" 2>/dev/null
        fi
    done
    HW_POLL_PID=""; HW_RAPL_PID=""
}

# Prints "hw_<metric> avg|min|max: value" for every metric found in <sample_file>.
get_hw_metrics_summary() {
    local sample_file=$1
    if [ ! -s "$sample_file" ]; then echo "hw_sample_count: 0"; return; fi

    gawk '
    BEGIN {
        lbl["cpu_usage_idle"]                        = "cpu_util_pct"
        lbl["cpu_usage_user"]                        = "cpu_usage_user"
        lbl["cpu_usage_system"]                      = "cpu_usage_system"
        lbl["cpu_frequency_avg_frequency"]           = "cpu_freq_mhz"
        lbl["temp_temp"]                             = "cpu_temperature"
        lbl["mem_used_percent"]                      = "mem_used_percent"
        lbl["gpu_engine_usage_usage__ccs"]           = "gpu_compute_util_pct"
        lbl["gpu_engine_usage_usage__compute"]       = "gpu_compute_util_pct"
        lbl["gpu_engine_usage_usage__vcs"]           = "gpu_video_util_pct"
        lbl["gpu_engine_usage_usage__video"]         = "gpu_video_util_pct"
        lbl["gpu_engine_usage_usage__rcs"]           = "gpu_render_util_pct"
        lbl["gpu_engine_usage_usage__render"]        = "gpu_render_util_pct"
        lbl["gpu_engine_usage_usage__vecs"]          = "gpu_enhance_util_pct"
        lbl["gpu_engine_usage_usage__video-enhance"] = "gpu_enhance_util_pct"
        lbl["gpu_frequency__cur_freq"]               = "gpu_freq_mhz"
        lbl["gpu_power__gpu_cur_power"]              = "gpu_power_w"
        lbl["gpu_power__pkg_cur_power"]              = "pkg_power_w"
        lbl["rapl_power_w__psys"]                    = "rapl_psys_w"
        lbl["rapl_power_w__pkg"]                     = "rapl_pkg_w"
        lbl["rapl_power_w__core"]                    = "rapl_core_w"
        lbl["rapl_power_w__uncore"]                  = "rapl_uncore_w"
        lbl["npu_utilization"]                       = "npu_utilization"
        lbl["npu_frequency"]                         = "npu_frequency"
        lbl["npu_power"]                             = "npu_power"
        lbl["npu_temperature"]                       = "npu_temperature"
        lbl["npu_memory_mb"]                         = "npu_memory_mb"
        lbl["npu_bandwidth"]                         = "npu_bandwidth"

        scale["cpu_frequency_avg_frequency"] = 0.001   # kHz -> MHz
        GPU_POWER_CAP = 500.0
        sample_ct = 0; gpu_comb_sum = 0; gpu_comb_cnt = 0
        cur_ccs = ""; cur_vcs = ""
    }
    /^---$/ {
        if (cur_ccs != "" || cur_vcs != "") {
            ccs_v = (cur_ccs != "") ? (cur_ccs + 0) : 0
            vcs_v = (cur_vcs != "") ? (cur_vcs + 0) : 0
            combined = (ccs_v > vcs_v) ? ccs_v : vcs_v
            if (gpu_comb_cnt == 0 || combined < gpu_comb_min) gpu_comb_min = combined
            if (gpu_comb_cnt == 0 || combined > gpu_comb_max) gpu_comb_max = combined
            gpu_comb_sum += combined
            gpu_comb_cnt++
        }
        cur_ccs = ""; cur_vcs = ""
        sample_ct++
        next
    }
    /=/ {
        eq = index($0, "=")
        if (eq <= 1) next
        key = substr($0, 1, eq - 1)
        val = substr($0, eq + 1) * 1.0
        if (!(key in lbl)) next
        if (key == "gpu_power__gpu_cur_power" || key == "gpu_power__pkg_cur_power") {
            if (val <= 0 || val >= GPU_POWER_CAP) next
        }
        if (key == "gpu_engine_usage_usage__ccs" || key == "gpu_engine_usage_usage__compute") cur_ccs = val
        if (key == "gpu_engine_usage_usage__vcs" || key == "gpu_engine_usage_usage__video")   cur_vcs = val
        if (!(key in cnt)) { cnt[key]=0; raw_sum[key]=0; raw_min[key]=val; raw_max[key]=val }
        cnt[key]++
        raw_sum[key] += val
        if (val < raw_min[key]) raw_min[key] = val
        if (val > raw_max[key]) raw_max[key] = val
    }
    END {
        print "hw_sample_count: " sample_ct
        if (sample_ct == 0) exit
        split("", printed)
        for (key in cnt) {
            friendly = lbl[key]
            if (friendly in printed) continue
            n = cnt[key]
            s = (key in scale) ? scale[key] : 1.0
            if (key == "cpu_usage_idle") {
                avg_v = 100.0 - (raw_sum[key] / n); min_v = 100.0 - raw_max[key]; max_v = 100.0 - raw_min[key]
            } else {
                avg_v = (raw_sum[key] / n) * s; min_v = raw_min[key] * s; max_v = raw_max[key] * s
            }
            printf "hw_%s avg: %.3f\n", friendly, avg_v
            printf "hw_%s min: %.3f\n", friendly, min_v
            printf "hw_%s max: %.3f\n", friendly, max_v
            printed[friendly] = 1
        }
        if (gpu_comb_cnt > 0) {
            printf "hw_gpu_util_combined avg: %.3f\n", gpu_comb_sum / gpu_comb_cnt
            printf "hw_gpu_util_combined min: %.3f\n", gpu_comb_min
            printf "hw_gpu_util_combined max: %.3f\n", gpu_comb_max
        }
    }
    ' "$sample_file"
}

# Value of "<key>" (e.g. "hw_cpu_util_pct avg:") in a kpi file, or N/A.
kpi_get() {
    local v
    v=$(grep -m1 -F "$2" "$1" 2>/dev/null | awk '{print $NF}')
    echo "${v:-N/A}"
}

# ═══════════════════════════════════════════════════════════════════════════════
#  Throughput analysis (per stream: mean of the instantaneous FPS in the window)
# ═══════════════════════════════════════════════════════════════════════════════
write_analyzer() {
    cat > "${WORK_DIR}/analyze.py" << 'PYEOF'
import json, statistics, sys
path, n = sys.argv[1], int(sys.argv[2])
fps, last_avg, lost, msgs = {}, {}, set(), {}
for line in open(path):
    try:
        sample = json.loads(line)
    except ValueError:
        continue
    for s in sample["s"]:
        sid = s["id"]
        if s["state"] == "RUNNING":
            inst = s.get("frame_fps") or s.get("avg_fps") or 0
            fps.setdefault(sid, []).append(float(inst))
            last_avg[sid] = float(s.get("avg_fps") or 0)
        else:
            lost.add(sid)
            if s.get("message"):
                msgs[sid] = s["message"]
ok = [sid for sid in fps if sid not in lost]
thr = sorted(statistics.fmean(fps[sid]) for sid in ok)
avg = sorted(last_avg[sid] for sid in ok)
print("streams_requested: %d" % n)
print("streams_running: %d" % len(ok))
for i, v in enumerate(thr, 1):
    print("throughput #%d: %.3f" % (i, v))
complete = len(ok) == n and thr
print("throughput min: %.3f" % (thr[0] if complete else 0))
print("throughput median: %.3f" % (statistics.median(thr) if thr else 0))
print("throughput average: %.3f" % (statistics.fmean(thr) if thr else 0))
print("throughput max: %.3f" % (thr[-1] if thr else 0))
print("throughput stdev: %.3f" % (statistics.pstdev(thr) if len(thr) > 1 else 0))
print("throughput cumulative: %.3f" % sum(thr))
print("avg_fps min: %.3f" % (avg[0] if avg else 0))
for m in sorted(set(msgs.values()))[:3]:
    print("stream message: " + m.replace("\n", " ")[:200])
PYEOF
}

# ═══════════════════════════════════════════════════════════════════════════════
#  Pipeline control
# ═══════════════════════════════════════════════════════════════════════════════
LAUNCHED_IDS=()
LAUNCHED_JSON="[]"
declare -A RESULT_PASS RESULT_MIN RESULT_TOTAL RESULT_DIR

active_count() {
    get_status | jq '[.[] | select(.state=="RUNNING" or .state=="QUEUED" or .state=="STARTING")] | length' 2>/dev/null || echo 0
}

stop_all_pipelines() {
    local ids id pids=() waited=0
    ids=$(get_status | jq -r '.[] | select(.state=="RUNNING" or .state=="QUEUED" or .state=="STARTING") | .id' 2>/dev/null)
    if [ -z "$ids" ]; then return 0; fi

    echo ">>>>> Stopping $(wc -l <<< "$ids") active/queued pipeline instance(s)..."
    for id in $ids; do
        api -X DELETE "${BASE_URL}/pipelines/${id}" > /dev/null &
        pids+=("$!")
    done
    wait "${pids[@]}" 2>/dev/null

    while [ "$(active_count)" -gt 0 ] && [ "$waited" -lt 90 ]; do
        sleep 1
        waited=$((waited + 1))
    done
    if [ "$(active_count)" -gt 0 ]; then echo "[WARN] Some instances did not stop within 90s."; fi
    sleep 2   # let the devices release their resources
}

build_payload() {
    local x=$1 video=$2
    jq -n -c \
        --arg video "$video" \
        --arg label "bench-${LABEL_TAG}-${x}" \
        --arg det_model "${MODELS_DIR}/${DET_MODEL}" \
        --arg inst "$DET_INSTANCE_ID" \
        --arg path "uav-bench-${RUN_TAG}-${x}" \
        --arg thickness "$BBOX_THICKNESS" \
        --argjson cap "$CAPTION_ENABLED" \
        --arg vlm "$VLM_PATH" \
        --arg vlm_inst "$VLM_INSTANCE_ID" \
        --arg prompt "$CAP_PROMPT" \
        --argjson tokens "$CAP_TOKENS" \
        --arg topic "uav/caption/bench_${x}" '
        {
          destination: { frame: { type: "rtsp", path: $path, encoding: "h264",
                                  "overlay-properties": { thickness: $thickness } } },
          parameters: ({ video: $video, detection_model: $det_model,
                         detection_instance_id: $inst, overlay_label: $label }
                       + (if $cap then { captioner_model_path: $vlm, captioner_instance_id: $vlm_inst, captioner_prompt: $prompt,
                                         captioner_max_new_tokens: $tokens, mqtt_topic: $topic }
                          else {} end))
        }'
}

launch_streams() {
    local n=$1 x payload resp code body id
    LAUNCHED_IDS=()
    for x in $(seq 1 "$n"); do
        payload=$(build_payload "$x" "${VIDEOS[$(( (x - 1) % ${#VIDEOS[@]} ))]}")
        resp=$(api -w $'\n%{http_code}' -X POST -H "Content-Type: application/json" \
                   -d "$payload" "${BASE_URL}/pipelines/user_defined_pipelines/${PIPELINE_NAME}")
        code=${resp##*$'\n'}
        body=${resp%$'\n'*}
        if [ "$code" != "200" ] && [ "$code" != "201" ]; then
            echo "[ERROR] Stream #$x was rejected (HTTP $code): $body"
            return 1
        fi
        id=$(tr -d '"[:space:]' <<< "$body")
        LAUNCHED_IDS+=("$id")
        sleep 0.2
    done
    LAUNCHED_JSON=$(printf '%s\n' "${LAUNCHED_IDS[@]}" | jq -R . | jq -sc .)
}

# Returns 0 when all launched streams are RUNNING, 1 on failure or timeout.
wait_for_running() {
    local n=$1 timeout=$((120 + 5 * $1)) waited=0 running bad msg
    echo -n ">>>>> Waiting for ${n} stream(s) to reach RUNNING"
    while [ "$waited" -lt "$timeout" ]; do
        read -r running bad < <(get_status | jq -r --argjson ids "$LAUNCHED_JSON" '
            [.[] | select(.id as $i | $ids | index($i))] as $s
            | [($s | map(select(.state=="RUNNING")) | length),
               ($s | map(select(.state=="ERROR" or .state=="ABORTED" or .state=="COMPLETED")) | length)] | @tsv')
        if [ "${bad:-0}" -gt 0 ]; then
            msg=$(get_status | jq -r --argjson ids "$LAUNCHED_JSON" \
                '[.[] | select(.id as $i | $ids | index($i)) | select(.state=="ERROR" or .state=="ABORTED") | .message] | map(select(. != "")) | .[0] // ""')
            echo " [FAILED: ${bad} stream(s) in ERROR/ABORTED${msg:+: ${msg:0:200}}]"
            return 1
        fi
        if [ "${running:-0}" -ge "$n" ]; then echo " [RUNNING]"; return 0; fi
        echo -n "."
        sleep 2
        waited=$((waited + 2))
    done
    echo " [TIMEOUT: ${running:-0}/${n} running after ${timeout}s]"
    return 1
}

sample_window() {
    local out=$1 end=$((SECONDS + WINDOW_SECONDS))
    while [ "$SECONDS" -lt "$end" ]; do
        get_status | jq -c --argjson ids "$LAUNCHED_JSON" \
            '{t: (now | floor), s: [.[] | select(.id as $i | $ids | index($i)) | {id, state, avg_fps, frame_fps, message}]}' >> "$out"
        printf '\r      measuring... %ss left ' "$((end - SECONDS))"
        sleep "$SAMPLE_INTERVAL"
    done
    printf '\r%*s\r' 40 ''
}

# run_workload <streams>  ->  sets EVAL_PASS (1/0), EVAL_MIN, EVAL_TOTAL, EVAL_DIR
run_workload() {
    local n=$1
    EVAL_PASS=0; EVAL_MIN=0; EVAL_TOTAL=0
    RUN_TAG=$(date +%s | tail -c 7)   # the server keeps stream paths of failed runs registered
    EVAL_DIR="${RUN_DIR}/streams_$(printf '%02d' "$n")"
    rm -rf "$EVAL_DIR"; mkdir -p "$EVAL_DIR"

    echo
    echo "Invoking workload with ${n} stream(s) on pipeline '${PIPELINE_NAME}'..."
    stop_all_pipelines
    if ! api --fail -o /dev/null "${BASE_URL}/pipelines/status"; then
        echo "[WARN] Pipeline server not answering; waiting for it to come back..."
        local w=0
        until api --fail -o /dev/null "${BASE_URL}/pipelines/status" || [ "$w" -ge 180 ]; do sleep 3; w=$((w + 3)); done
    fi

    RUN_ACTIVE=true
    local t0=$SECONDS
    if launch_streams "$n" && wait_for_running "$n"; then
        echo ">>>>> Warm-up ${WARMUP_SECONDS}s, then sampling ${WINDOW_SECONDS}s (startup took $((SECONDS - t0))s)"
        sleep "$WARMUP_SECONDS"
        start_hw_monitor "$EVAL_DIR"
        sample_window "$EVAL_DIR/status_samples.jsonl"
        stop_hw_monitor
        python3 "${WORK_DIR}/analyze.py" "$EVAL_DIR/status_samples.jsonl" "$n" > "$EVAL_DIR/kpi.txt"
        if $HW_MONITOR_ENABLED && [ -s "$EVAL_DIR/hw_samples.log" ]; then
            echo "---hw-metrics---" >> "$EVAL_DIR/kpi.txt"
            get_hw_metrics_summary "$EVAL_DIR/hw_samples.log" >> "$EVAL_DIR/kpi.txt"
        fi
        sed -n '/^throughput #/p;/^throughput min/p;/^throughput cumulative/p' "$EVAL_DIR/kpi.txt" \
            | sed "s|^|stream-density#${n}: |"
        grep -F 'stream message:' "$EVAL_DIR/kpi.txt"
        EVAL_MIN=$(kpi_get "$EVAL_DIR/kpi.txt" "throughput min:")
        EVAL_TOTAL=$(kpi_get "$EVAL_DIR/kpi.txt" "throughput cumulative:")
    else
        printf 'streams_requested: %s\nstreams_running: 0\nthroughput min: 0\n' "$n" > "$EVAL_DIR/kpi.txt"
        EVAL_MIN=0
        EVAL_TOTAL=0
    fi
    stop_hw_monitor
    stop_all_pipelines
    RUN_ACTIVE=false

    EVAL_PASS=$(awk -v f="$EVAL_MIN" -v t="$TARGET_FPS" 'BEGIN {print (f+0 >= t+0 && f+0 > 0) ? 1 : 0}')
    RESULT_PASS[$n]=$EVAL_PASS; RESULT_MIN[$n]=$EVAL_MIN
    RESULT_TOTAL[$n]=$EVAL_TOTAL; RESULT_DIR[$n]=$EVAL_DIR
    if [ "$EVAL_PASS" -eq 1 ]; then
        echo "  -> ${n} stream(s): min ${EVAL_MIN} fps >= ${TARGET_FPS}  PASS"
    else
        echo "  -> ${n} stream(s): min ${EVAL_MIN} fps <  ${TARGET_FPS}  FAIL"
    fi
}

# Exponential doubling, then bisect between the last pass and the first fail.
density_search() {
    local lo=$((MIN_STREAMS - 1)) hi=0 n=$MIN_STREAMS mid
    BEST_N=0
    while [ "$n" -le "$MAX_STREAMS" ]; do
        run_workload "$n"
        if [ "$EVAL_PASS" -eq 1 ]; then
            BEST_N=$n; lo=$n
            [ "$n" -ge "$MAX_STREAMS" ] && return
            n=$((n * 2)); [ "$n" -gt "$MAX_STREAMS" ] && n=$MAX_STREAMS
        else
            hi=$n
            break
        fi
    done
    [ "$hi" -eq 0 ] && return

    while [ $((hi - lo)) -gt 1 ]; do
        mid=$(( (lo + hi) / 2 ))
        run_workload "$mid"
        if [ "$EVAL_PASS" -eq 1 ]; then BEST_N=$mid; lo=$mid; else hi=$mid; fi
    done
}

# ═══════════════════════════════════════════════════════════════════════════════
#  Reporting
# ═══════════════════════════════════════════════════════════════════════════════
print_hw_detail() {
    local kpi=$1
    local rows=(
        "CPU utilisation (%)|hw_cpu_util_pct"
        "CPU frequency (MHz)|hw_cpu_freq_mhz"
        "CPU temperature (C)|hw_cpu_temperature"
        "GPU utilisation (%, max of compute/video)|hw_gpu_util_combined"
        "GPU compute engine (%)|hw_gpu_compute_util_pct"
        "GPU video decode/encode engine (%)|hw_gpu_video_util_pct"
        "GPU video-enhance engine (%)|hw_gpu_enhance_util_pct"
        "GPU render engine (%)|hw_gpu_render_util_pct"
        "GPU frequency (MHz)|hw_gpu_freq_mhz"
        "GPU power (W)|hw_gpu_power_w"
        "NPU utilisation (%)|hw_npu_utilization"
        "NPU frequency (MHz)|hw_npu_frequency"
        "NPU power (W)|hw_npu_power"
        "NPU temperature (C)|hw_npu_temperature"
        "NPU memory (MB)|hw_npu_memory_mb"
        "Package power, RAPL (W)|hw_rapl_pkg_w"
        "Package power, qmassa (W)|hw_pkg_power_w"
        "Platform power, RAPL psys (W)|hw_rapl_psys_w"
        "Memory used (%)|hw_mem_used_percent"
    )
    local row label key a mn mx
    printf "  %-44s %10s %10s %10s\n" "Hardware metric" "avg" "min" "max"
    for row in "${rows[@]}"; do
        label=${row%%|*}; key=${row##*|}
        a=$(kpi_get "$kpi" "${key} avg:")
        [ "$a" == "N/A" ] && continue
        mn=$(kpi_get "$kpi" "${key} min:"); mx=$(kpi_get "$kpi" "${key} max:")
        printf "  %-44s %10.1f %10.1f %10.1f\n" "$label" "$a" "$mn" "$mx"
    done
}

fmt1() { awk -v v="$1" 'BEGIN { if (v == "N/A") print "N/A"; else printf "%.1f", v }'; }

print_summary() {
    local n kpi pw
    echo
    echo "======================================================"
    echo " Results: ${PIPELINE_NAME}   (target >= ${TARGET_FPS} FPS/stream)"
    echo "======================================================"
    printf "  %7s %9s %10s %7s %7s %7s %9s %8s  %s\n" "Streams" "MinFPS" "TotalFPS" "CPU%" "GPU%" "NPU%" "Pkg(W)" "FPS/W" "Verdict"
    for n in $(printf '%s\n' "${!RESULT_DIR[@]}" | sort -n); do
        kpi="${RESULT_DIR[$n]}/kpi.txt"
        pw=$(kpi_get "$kpi" "hw_rapl_pkg_w avg:")
        [ "$pw" == "N/A" ] && pw=$(kpi_get "$kpi" "hw_pkg_power_w avg:")
        printf "  %7s %9.2f %10.2f %7s %7s %7s %9s %8s  %s\n" "$n" "${RESULT_MIN[$n]}" "${RESULT_TOTAL[$n]}" \
            "$(fmt1 "$(kpi_get "$kpi" "hw_cpu_util_pct avg:")")" \
            "$(fmt1 "$(kpi_get "$kpi" "hw_gpu_util_combined avg:")")" \
            "$(fmt1 "$(kpi_get "$kpi" "hw_npu_utilization avg:")")" \
            "$(fmt1 "$pw")" \
            "$(awk -v p="$pw" -v t="${RESULT_TOTAL[$n]}" 'BEGIN { if (p == "N/A" || p+0 == 0) print "N/A"; else printf "%.2f", t / p }')" \
            "$([ "${RESULT_PASS[$n]}" -eq 1 ] && echo PASS || echo FAIL)"
    done
    echo "------------------------------------------------------"

    if [ -n "$FIXED_STREAMS" ]; then
        BEST_N=$FIXED_STREAMS
        echo " Fixed run: ${FIXED_STREAMS} stream(s) -> $([ "${RESULT_PASS[$FIXED_STREAMS]}" -eq 1 ] && echo "sustains" || echo "does NOT sustain") ${TARGET_FPS} FPS"
    elif [ "$BEST_N" -gt 0 ]; then
        echo " FINAL RESULT: Stream-Density Benchmark Completed"
        echo "   Scenario       : Det=${DET_DEVICE} | Captioning=${CAPTION_ENABLED}$([ "$CAPTION_ENABLED" == "true" ] && echo " (${CAP_DEVICE})")"
        echo "   Pipeline       : ${PIPELINE_NAME}"
        echo "   Stream density : ${BEST_N} stream(s) @ >= ${TARGET_FPS} FPS"
        echo "   FPS/stream     : ${RESULT_MIN[$BEST_N]} (min)   Total: ${RESULT_TOTAL[$BEST_N]} FPS"
    else
        echo " FINAL RESULT: ${TARGET_FPS} FPS is not achievable with ${MIN_STREAMS} stream(s)."
    fi

    if $HW_MONITOR_ENABLED && [ "${BEST_N:-0}" -gt 0 ] && [ -f "${RESULT_DIR[$BEST_N]}/kpi.txt" ]; then
        echo
        echo " Hardware load at ${BEST_N} stream(s)  ($(kpi_get "${RESULT_DIR[$BEST_N]}/kpi.txt" "hw_sample_count:") samples)"
        print_hw_detail "${RESULT_DIR[$BEST_N]}/kpi.txt"
    fi
    echo "======================================================"
    echo " Raw data: ${RUN_DIR#${APP_ROOT}/}"
}

# ═══════════════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════════════
RUN_ACTIVE=false
cleanup() {
    stop_hw_monitor
    if $RUN_ACTIVE; then
        RUN_ACTIVE=false
        stop_all_pipelines
    fi
    rm -rf "$WORK_DIR"
}
trap cleanup EXIT
trap 'echo; echo "Interrupted."; exit 130' INT TERM

echo ">>>>> Performing pre-flight checks (${BASE_URL})..."
HTTP_CODE=$(curl -k -s --connect-timeout 5 -o /dev/null -w "%{http_code}" "${BASE_URL}/pipelines/status")
if [ "$HTTP_CODE" != "200" ]; then
    echo "[ERROR] DL Streamer Pipeline Server is not reachable at ${BASE_URL}/pipelines/status (HTTP ${HTTP_CODE})."
    echo "        Is the stack running and nginx published on HOST_IP? Override with -u <url>."
    exit 1
fi
echo "DL Streamer Pipeline Server is reachable (HTTP 200)."

resolve_scenario
check_pipeline_exists

if $HW_MONITOR_ENABLED; then
    if check_metrics_manager; then
        echo "HW metrics: ${METRICS_URL} (every ${METRICS_INTERVAL}s)"
        write_hw_helpers
    else
        echo "[WARN] metrics-manager not reachable at ${METRICS_URL}; CPU/GPU/NPU/power metrics disabled."
        HW_MONITOR_ENABLED=false
    fi
fi
write_analyzer

RUN_DIR="${RESULTS_ROOT}/$(date +%Y%m%d_%H%M%S)_${PIPELINE_NAME}"
mkdir -p "$RUN_DIR"

BEST_N=0
if [ -n "$FIXED_STREAMS" ]; then
    run_workload "$FIXED_STREAMS"
else
    echo "Start-Trace:"
    density_search
    echo "Stop-Trace:"
fi

print_summary | tee "${RUN_DIR}/summary.txt"

<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Benchmark — UAV Vision Analytics

> **Scope:** This benchmarking guide applies to **Standalone Mode (pymavlink)** only. It does not cover the UAV Mission Compute SDK mode.

This document explains how to measure the performance of the UAV Vision Analytics
application using the `calc_stream_density.sh` benchmarking script. The script
determines the maximum number of concurrent drone-camera video streams the
system can process (**stream density**) while sustaining a target frame rate,
and simultaneously collects CPU, GPU and NPU utilization, frequency, power and memory
metrics from `metrics-manager`.

> [!NOTE]
> Other benchmarking options are also offered by Open Edge Platform. Use
> [Visual Pipeline and Platform Evaluation Tool (ViPPET)](https://docs.openedgeplatform.intel.com/dev/edge-ai-libraries/visual-pipeline-and-platform-evaluation-tool/index.html)
> for comparing AI pipeline performance across CPU, GPU, and NPU.
>
> For platform-level benchmarking, see the [Edge Workloads and Benchmarks Guide](./run-edge-benchmarks.md) -
> a solution for end-to-end video analytics pipelines, vision AI inference,
> hardware-accelerated media processing, and generative AI.

## Prerequisites

1. The stack is running (`make pymav-up`) and the detection model exists
   (`resources/models/yolo11s/yolo11s_openvino_model/yolo11s.xml`, `make model`). For captioning
   scenarios the VLM must exist too (`make vlm-model`).
2. Host tools: `curl`, `jq`, `gawk`, `python3` (with PyYAML to read `configs/cameras-pymavlink.yaml`).
3. All requests go through the `nginx` reverse proxy (`https://<HOST_IP>`, self-signed certificate, so
   the script skips certificate verification). The address is detected automatically
   (`docker port nginx`, then `HOST_IP` in `.env`).

```bash
curl -k -s https://<HOST_IP>/pipelines/status | head -3
curl -k -s https://<HOST_IP>/api/v1/metrics/latest | head -3
```

## How the Script Works

1. **Scenario** — the detection device, captioning on/off, the captioning device, the models, the prompt and
   `max_new_tokens` come from the first enabled video camera in `configs/cameras-pymavlink.yaml`.
   Flags override them, so no file has to be edited to try another combination.
   The pipeline is chosen like the application does:
   `uav_object_detection_<det>` (captioning off) or `uav_object_detection_<det>_<vlm>` (captioning on).
2. **Requests** — each stream is started with the same request the pipeline manager sends for a camera
   (`detection_model`, `detection_instance_id`, captioner parameters, `h264` RTSP output). All streams of a run
   share **one detection model instance** (`inst<det>_<model>`) and, with captioning, **one VLM instance**
   (`instvlm_<device>_<model>_t<tokens>`), exactly as the application does.
3. **Search** — exponential doubling (1, 2, 4, 8 …) up to `-m`, then bisect between the last passing and the
   first failing stream count. A count passes when **every** stream sustains at least `-t` FPS and all
   streams stay `RUNNING`.
4. **Measurement** — after all streams are `RUNNING` and a warm-up (`--warmup`, default 10 s), the script samples
   `/pipelines/status` every 2 s for `-w` seconds (default 60 s). Per stream it takes the mean of the
   instantaneous FPS in the window. Hardware metrics are collected for the same window.
5. **Cleanup** — all active pipelines are stopped before each run and when the script exits or is interrupted.

## Scenarios

```bash
cd federal-and-aerospace-ai-suite/uav-vision-analytics/benchmark

# 1. Whatever cameras-pymavlink.yaml says
./calc_stream_density.sh

# 2. Detection only (3 cases)
./calc_stream_density.sh -d CPU -c false
./calc_stream_density.sh -d GPU -c false
./calc_stream_density.sh -d NPU -c false

# 3. Detection + captioning on CPU (3 cases)
./calc_stream_density.sh -d CPU -c true -cd CPU
./calc_stream_density.sh -d GPU -c true -cd CPU
./calc_stream_density.sh -d NPU -c true -cd CPU

# 4. Detection + captioning on GPU (3 cases)
./calc_stream_density.sh -d CPU -c true -cd GPU
./calc_stream_density.sh -d GPU -c true -cd GPU
./calc_stream_density.sh -d NPU -c true -cd GPU
```

More useful runs:

```bash
# One fixed run with 8 streams (no search) — quick way to compare pipeline changes
./calc_stream_density.sh -d NPU -c false -n 8 -w 30

# Shorter captions (captioning load grows with max_new_tokens)
./calc_stream_density.sh -d NPU -c true -cd GPU -ct 50

# List the pipelines registered in the pipeline server
./calc_stream_density.sh -l
```

## CLI Reference

| Flag | Default | Description |
| --- | --- | --- |
| `-d`, `--det-device` | YAML | Detection device `CPU`, `GPU` or `NPU`. |
| `-c`, `--captioning` | YAML | `true` or `false`. |
| `-cd`, `--cap-device` | YAML | Captioning device `CPU` or `GPU` (the VLM does not run on the NPU). |
| `-ct`, `--cap-tokens` | YAML | Caption length limit (`max_new_tokens`). |
| `-p`, `--pipeline` | derived | Use this pipeline name instead. |
| `-t`, `--target-fps` | `28.5` | FPS every stream must sustain. |
| `-s`, `--min-streams` | `1` | First stream count tried. |
| `-m`, `--max-streams` | `64` | Upper limit of the search. |
| `-n`, `--streams` | — | Run exactly this many streams once, without search. |
| `-w`, `--window` | `60` | Measurement window per run, seconds. |
| `--warmup` | `10` | Wait between "all streams running" and the start of the measurement, seconds. |
| `-u`, `--url` | detected | Pipeline server URL behind nginx. |
| `--metrics-url` | same host | `metrics-manager` URL behind nginx. |
| `--metrics-interval` | `2` | Polling interval if only the REST endpoint is available. |
| `--no-hw-metrics` | off | Skip hardware metrics. |
| `-l`, `--list-pipelines` | — | List registered pipelines and exit. |
| `-h`, `--help` | — | Usage. |

Environment variables: `DLSPS_NODE_IP` (nginx address), `NGINX_CONTAINER` / `METRICS_CONTAINER`
(default `uav-va-nginx` / `uav-va-metrics-manager`), `CAMERAS_CONFIG` (another YAML), `RESULTS_DIR`
(output folder), `BBOX_THICKNESS` (same as the application, default 6).

## Understanding the Output

```text
======================================================
 Results: uav_object_detection_npu_gpu   (target >= 28.5 FPS/stream)
======================================================
  Streams    MinFPS   TotalFPS    CPU%    GPU%    NPU%    Pkg(W)    FPS/W  Verdict
        1     <fps>      <fps>    <%>     <%>     <%>      <W>     <fps/W>  PASS
        2     <fps>      <fps>    <%>     <%>     <%>      <W>     <fps/W>  PASS
        3     <fps>      <fps>    <%>     <%>     <%>      <W>     <fps/W>  FAIL
------------------------------------------------------
 FINAL RESULT: Stream-Density Benchmark Completed
   Stream density : <N> stream(s) @ >= 28.5 FPS

 Hardware load at <N> stream(s)  (<samples> samples)
  Hardware metric                                     avg        min        max
  CPU utilisation (%)                                <%>        <%>        <%>
  GPU compute engine (%)                             <%>        <%>        <%>
  NPU utilisation (%)                                <%>        <%>        <%>
  ...
```

The hardware table lists, when available: CPU utilisation, frequency and temperature; GPU utilisation
(`max(compute, video)`), the compute (CCS), video decode/encode (VCS), video-enhance (VECS) and render engines,
GPU frequency and power; NPU utilisation, frequency, power, temperature and memory; package power (RAPL and
qmassa), platform power and memory use. FPS/W is the total FPS divided by the package power.

### Output folder

Every run writes to `benchmark/results/<timestamp>_<pipeline>/` (ignored by git):

```text
summary.txt                  the table printed at the end
streams_<NN>/kpi.txt         throughput #i, min/median/average/max/stdev/cumulative, hw_* avg/min/max
streams_<NN>/status_samples.jsonl   /pipelines/status samples of the window
streams_<NN>/hw_samples.log  raw hardware snapshots (key=value, "---" per snapshot)
```

## Getting More Streams

- **Captioning dominates the cost.** A VLM generating long captions keeps the GPU busy even with one or two
  streams. Lower `captioning.max_new_tokens` in `cameras-pymavlink.yaml` (or try `-ct`), caption only the cameras
  that need it, or run the VLM on a different device than the detector.
- **Cameras share one VLM.** Cameras captioning with the same device, model and token limit share one loaded
  VLM (`captioner_instance_id`, set by the camera manager), so memory does not grow with every captioning camera.
  Their captions are generated one at a time, so each camera is captioned less often when many caption at once.
- **RTSP output and overlay are part of the load.** Cameras with `overlay: false` in the YAML are cheaper.
- **Repeat runs.** Results vary by 10–15 % from run to run (thermal and power state). Use `-w 60` or longer and
  repeat before drawing conclusions.
- Benchmark results vary by 10–15 % from run to run (thermal and power state). Use `-w 60` or longer and
  repeat before drawing conclusions.

## Troubleshooting

For benchmark-related troubleshooting (missing tools, connectivity issues, result anomalies, GPU/NPU
visibility, and power readings), see the
[Troubleshooting guide](../how-to-guides/troubleshooting.md#benchmark).

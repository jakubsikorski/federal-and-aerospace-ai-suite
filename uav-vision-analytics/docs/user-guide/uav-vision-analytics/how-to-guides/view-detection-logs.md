<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Read and Filter Detection Logs

Every pipeline can write its detection results as newline-delimited JSON
(`json-lines`) to a file inside the container — a durable, machine-readable
record of what the app detected, independent of the RTSP video stream.

## Prerequisites

- A pipeline started **by hand** with a `destination.metadata` block. The
  camera-file manager (`make start-rtsp`) does not add one — its pipelines
  publish no per-frame log. Start the
  pipeline through the REST API as shown in
  [Manage Pipelines — Start a pipeline](../how-to-customize/pipeline-management/manage-pipelines.md#start-a-pipeline).

## How it's configured

Add this to the start request's `destination`:

```json
"destination": {
  "metadata": {
    "type": "file",
    "path": "/home/pipeline-server/resources/results.jsonl",
    "format": "json-lines"
  },
  "frame": {"type": "rtsp", "path": "uav-my-camera", "encoding": "h264"}
}
```

Several pipelines may write to the same `path` (lines interleave) or each
to its own, e.g., `/home/pipeline-server/resources/results_front.jsonl`. The
server only accepts a `path` inside `/home/pipeline-server/resources/` (the
mounted `resources` folder); any other location, such as `/tmp`, is rejected with
`Invalid destination.metadata.path`. The file is written inside the container;
copy it out with `docker cp` as shown below.

## Steps

### 1. Tail the log live

```bash
docker exec uav-va-dlstreamer-pipeline-server tail -f /home/pipeline-server/resources/results.jsonl
```

### 2. Pretty-print and filter with `jq`

Each line is one frame's metadata: a timestamp, resolution, and (if any
objects were detected) an `objects` array with bounding boxes, class labels,
and confidence scores.

```bash
# Pretty-print the most recent line
docker exec uav-va-dlstreamer-pipeline-server sh -c 'tail -n 1 /home/pipeline-server/resources/results.jsonl | jq .'

# Show only frames where at least one object was detected
docker exec uav-va-dlstreamer-pipeline-server sh -c "tail -n 200 /home/pipeline-server/resources/results.jsonl | jq -c 'select(any(.objects[]; .detection.confidence > 0)) | {ts: .timestamp, objects: [.objects[] | select(.detection.confidence > 0) | .detection.label]}'"
```

> [!NOTE]
> `objects` also lists the telemetry text lines drawn on the frame (`Name`,
> `Frame`, `ALT`, ...) with confidence `0`; the filter above skips them. `jq`
> runs inside the container, which already has it available. To run it
> from the host instead, see
> [Troubleshooting — jq: command not found](../troubleshooting.md#jq-command-not-found).

### 3. Copy the log out for offline analysis

```bash
docker cp uav-va-dlstreamer-pipeline-server:/home/pipeline-server/resources/results.jsonl ./results.jsonl
```

### 4. Correlate a log entry with a pipeline instance

The metadata file itself does not include the pipeline `instance_id`. To
confirm *which* running pipeline is currently writing to a given path, check
`GET /pipelines/status` (see
[Manage Pipelines — Check status](../how-to-customize/pipeline-management/manage-pipelines.md#check-status))
and match on the `destination.metadata.path` you started it with.

## Verify

- The file grows while a pipeline is `RUNNING` (`ls -la /home/pipeline-server/resources/results.jsonl`
  inside the container shows an increasing size / mtime).
- `jq` parses every line without error (`jq . < results.jsonl > /dev/null`
  reports no parse errors).

## Troubleshooting

A missing or empty log file is covered in
[Troubleshooting — Detection log (JSONL) is missing or empty](../troubleshooting.md#detection-log-jsonl-is-missing-or-empty);
`jq` on the host in [jq: command not found](../troubleshooting.md#jq-command-not-found).
The JSONL carries no pipeline or camera name (the overlay label is drawn on
the video only) — give each pipeline its own `path` to tell them apart.

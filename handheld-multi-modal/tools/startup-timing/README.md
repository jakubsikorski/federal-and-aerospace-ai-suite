<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Startup timing

Times the steps of any startup command and writes a short, readable report. Python 3.11+, standard
library only, Bash helpers for the call sites. It needs no configuration file.

## Use it in three steps

1. Wrap the command: `tools/startup-timing/timing.sh run --name myapp -- ./start.sh`
2. Time the steps inside your scripts or Make recipes (this file only needs to be sourced):

   ```bash
   source tools/startup-timing/timing.sh
   timed setup 'Generate TLS certificate' ./make-cert.sh        # runs and times a command
   timed --service backend build 'Build backend image' docker build .   # optional owner tag
   timing_cached models 'Model cache is current'                # work skipped because cached
   token=$(timing_begin start 'Wait for the API'); ...; timing_end "$token" --code "$?"
   ```

3. Open `startup-timings/latest/report.md`.

The **label** (second argument) is the row text people read, written once at the call site. The
**group** (first argument) is the section name; `model_download` shows as "Model Download" and
acronyms stay as written (`API_start` shows as "API Start"). The
optional `--service NAME` (before the group) tags a step with the service that owns it. The helpers do nothing outside `timing.sh run`, so scripts stay runnable by
themselves. A step timed inside another timed command is nested under it automatically.

The helpers are best-effort: a timing fault (unwritable directory, odd label) prints a warning and
never fails the application, even under `set -e`. `timed` is for **external commands**; running a
shell function through it disables `set -e` inside that function. For a function, call
`timing_begin` before and `timing_end "$token" --code "$?"` after it.

## What the report shows

`report.md` reads top to bottom, and the first two parts are short enough to paste into a cycle report
(`timing.sh summary` prints just those):

- **Headline:** result, total time, cold or warm start, then up to three sentences: the kind of
  work that took longest (with its share of the run), the next one, and the busiest service.
- **Time by activity:** one row per kind of work (the step *group*, e.g. container image
  downloads, model downloads, builds, readiness waits), longest first. Parallel steps of the same
  kind are counted once, so "13 image downloads: 21m, 15 GB at about 12 MB/s" is one row, not
  thirteen overlapping ones. This is where "most of the time was downloads" shows up.
- **Time by service:** one row per service, busiest first: time, share and biggest cost. Steps in a
  parallel batch (three or more steps of one kind running together, such as image pulls that share
  the network) are left out here, because each would otherwise look busy for the whole batch.
  Shares can add up to more than 100% because services run in parallel.
- **When each service was busy:** a text chart, one bar per service, so overlaps are visible.
- **What each service spent its time on:** the five largest steps per service as a short list.
- **All timed steps** (collapsed): every step by start time, nested steps indented.
- A step that never ended is shown as **unavailable**, never as zero. A failed run still writes a
  partial report that names the failed step. Commands, environment variables and output are never stored.

Pick group names that say what kind of work it is (`container_download`, `model_download`,
`build`, `readiness`): they become the *Time by activity* rows. The `pull` command records image sizes, which adds size and speed. Tag steps with the service that owns them (`timed --service db ...`); steps without one are
grouped as *Shared setup*. Write labels for a reader: "Download model neural-chat-7b", not a log
phrase. The console summary at the end of a run shows the top five services.

## For other tools: `summary.json`

Every run also writes a small, stable `summary.json` next to `report.md` (`timing.sh summary --json`
prints it). Use it, not `report.json`, to feed dashboards or cycle reports. Keys are not renamed
within `startup-timing-summary/1`:

```json
{"schema": "startup-timing-summary/1", "name": "demo-up", "status": "ok", "returncode": 0,
 "started_at": "2026-11-25T11:36:39Z", "total_seconds": 1155.7, "run_type": "cold",
 "idle_seconds": 3.1,
 "activities": [{"group": "container_download", "name": "Container image downloads",
                  "wall_seconds": 1266.0, "share": 0.73, "steps": 13, "bytes": 15000000000, "parallel": true}],
 "bottleneck": {"id": "chatqna", "name": "Chat Q&A", "busy_seconds": 1112.4, "share": 0.9625,
                "first_seconds": 3.8, "last_seconds": 1116.2, "cached_steps": 0, "failed_steps": 0,
                "work": [{"label": "Download model neural-chat-7b", "seconds": 737.7}]},
 "services": ["same shape as bottleneck, busiest first"]}
```

`run_type` is `cold`, `warm`, `partly warm` or `unknown`. `share` can exceed 1 in total because
services overlap.

## What works for other applications

Works as is for any application that starts through a bash script or Make recipe on a host with
Python 3.11+: wrap steps with `timed`, optionally tag `--service`, run under `timing.sh run`.
No config file is needed. Non-bash launchers can call `python3 timing.py begin ...` and
`... end TOKEN` (the same two commands the helpers use).

Not covered: a service timing its own startup from inside a container (the journal is a host file
reached through `STARTUP_TIMING_DIR`; mount it yourself if you need that), and the Handheld
log-marker rules in `startup-timing.toml`, which only fit those stock images. Other applications
should leave `[[log_rules]]` out.

`--explain` adds per-section totals (sum of step times, and wall time with overlaps merged) and
service descriptions. `--gantt` adds a Mermaid gantt chart. The full data (ids, parents, offsets,
timestamps, methods) is in `report.json`.

## Commands

| Command | Purpose |
|---|---|
| `timing.sh run [--name N] [--config F] [--output DIR] [--explain] [--gantt] [--no-logs] -- CMD...` | Run, then write `report.md`, `report.json` and a `latest` symlink; prints a short summary |
| `timing.sh compare A B` | Per-step deltas; A and B are `report.json` files or run directories (for example `startup-timings/latest`) |
| `timing.sh render [--explain] [--gantt] REPORT.json...` | Rebuild `report.md` from `report.json` (older reports render too) |
| `timing.sh begin/end` | What the Bash helpers call |

A command killed by a signal exits with 128 plus the signal number (130 for Ctrl-C, 143 for SIGTERM).
Reports go to `./startup-timings/<UTC time with microseconds>-<id>/`, which sort oldest first, but
prefer `compare <named run> startup-timings/latest` over relying on name order. The end of a run
prints the exact `compare` command when a previous run exists. Override the location with
`--output`, `STARTUP_TIMING_ROOT`, or `[report]` in the config. The wrapper executes the command, so do not put it in front of a dry run.

## Optional configuration

`startup-timing.toml` in the working directory, `STARTUP_TIMING_CONFIG` or `--config`. It is polish
only:

```toml
[groups]                       # display titles for groups
model_download = "Model downloads"
[services.backend]             # display title and --explain description for --service
title = "Backend"
description = "Runs application logic."
[report]                       # report location; root_env names an env var holding the base dir
directory = "startup-timings"
```

### Service-log plugin (optional, Handheld-specific)

Loaded only when the config has `[[log_rules]]` or a service with `container`. After the run it
reads those containers' Docker logs, turns configured start/end markers into steps (marked with a
dagger in the report, nested under a recorded step of the same service when it fits) and never
stores raw logs. Rules: `service`, `pattern`, `action` (`start`, `close`, `skip`, `close_or_skip`),
`key`, `group`, `label`; see `handheld-multi-modal/startup-timing.toml`. `[ready_patterns]` closes
open phases when the service says it is ready. A missing end marker is reported as unavailable.
`timing.sh pull -- <cmd listing images>` (plugin, `[image_services]`, `[pull]`; `max_parallel` defaults to 4) pulls missing images
in parallel and times each. Disable the log reading with `--no-logs`.

## Layout

`timing.sh` (helpers and CLI), `timing.py` (entry point), `startup_timing/`: `journal.py`
(recorder), `report.py` (build/normalise `report.json`), `render.py` (Markdown, console),
`compare.py`, `config.py`, `logmarkers.py` and `images.py` (plugins), `cli.py`. Copy the whole
`tools/startup-timing` directory into the application that uses it. In this repository the
copy lives inside the Handheld application so it ships with every install; the Makefile
runs without timing if the directory is missing.

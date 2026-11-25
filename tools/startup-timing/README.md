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

- **Headline:** result, total time, slowest step, cold or warm run (how many steps were cached or
  already running), step counts.
- **Timeline:** one table sorted by start time with start offset, duration, status, a bar showing
  where in the run each step sat, nested steps indented, and a closing *Unmeasured / idle* row
  (run time covered by no step) so the rows add up to the total. If steps ran in parallel a note
  says their times add up to more than the run.
- **Slowest 5:** the slowest leaf steps.
- A step that never ended is shown as **unavailable**, never as zero. A failed run still writes a
  partial report. Commands, environment variables and output are never stored.

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
`timing.sh pull -- <cmd listing images>` (plugin, `[image_services]`, `[pull]`) pulls missing images
in parallel and times each. Disable the log reading with `--no-logs`.

## Layout

`timing.sh` (helpers and CLI), `timing.py` (entry point), `startup_timing/`: `journal.py`
(recorder), `report.py` (build/normalise `report.json`), `render.py` (Markdown, console),
`compare.py`, `config.py`, `logmarkers.py` and `images.py` (plugins), `cli.py`. Copy the whole
`tools/startup-timing` directory when packaging an application separately.

---
# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
name: startup-timing
description: "Use when adding startup or bring-up timing to an application (make up, start scripts, docker compose launch), when reading, explaining or comparing a startup timing report (report.md, report.json, startup-timings/latest), or when asked why startup is slow or whether a run was cold or warm. Covers tools/startup-timing: the timed/timing_cached/timing_begin/timing_end Bash helpers, the `timing.sh run` wrapper, the optional startup-timing.toml and the Handheld log-marker plugin. Not for runtime performance, latency or throughput profiling."
---

# Startup timing

Shared tool: `tools/startup-timing/` (Python 3.11+, stdlib only; full docs in its `README.md`).
It times the steps of a startup command and writes a human-readable report. No config file needed.

## Add timing to a new app (zero config)

1. Wrap the command (this EXECUTES it, so never put it in front of a dry run):

   ```bash
   tools/startup-timing/timing.sh run --name myapp -- ./start.sh
   ```

2. In the scripts or Make recipes, source the helpers and wrap steps:

   ```bash
   source tools/startup-timing/timing.sh      # sets no shell options
   timed setup 'Generate TLS certificate' ./make-cert.sh
   timed --service backend build 'Build backend image' docker build .
   timing_cached models 'Model cache is current'          # skipped because cached
   token=$(timing_begin start 'Wait for the API'); wait_for_api; timing_end "$token" --code "$?"
   ```

   - Syntax: `timed [--service NAME] GROUP LABEL CMD...` (returns CMD's exit code),
     `timing_begin [--service NAME] GROUP LABEL` (prints a token),
     `timing_end TOKEN [--code N] [--status ok|failed|cached|reused]`,
     `timing_cached [--service NAME] GROUP LABEL`.
   - `timed` is for external commands only: a shell function run through it loses `set -e`
     inside the function. For a function use `timing_begin` / `timing_end` around the call.
   - The helpers are best-effort (a timing fault warns on stderr and never fails the app) and
     accept labels starting with `-`.
   - LABEL is the exact row text shown to people. GROUP is the section name; free text, so
     `model_download` shows as "Model Download"; acronyms like `API` are kept.
   - A timed step started inside another timed command nests under it automatically.
   - In a Makefile: `timing = source "$(abspath ../tools/startup-timing)/timing.sh"` and use
     `@$(timing); timed setup 'X' cmd` inside one shell recipe.

3. Open `startup-timings/latest/report.md` (the end of the run also prints a ~10 line summary and the path).

Verify quickly: run a script with two `timed ... sleep 0.2` lines and check both rows appear.

## Where reports land

`<root>/startup-timings/<UTC time with microseconds>-<id>/{report.md,report.json,events.jsonl}` plus a relative
`latest` symlink. Root is `--output`, else `$STARTUP_TIMING_ROOT`, else `[report]` in the config,
else the current directory. `report.json` is the complete data; `events.jsonl` is the raw journal.
Reports never store commands, environment or output.

## Other commands

| Command | Use |
|---|---|
| `timing.sh run [--name N] [--config F] [--output DIR] [--explain] [--gantt] [--no-logs] -- CMD...` | run and report; exit code is CMD's; a report is written even on failure |
| `timing.sh compare A B` | per-step deltas, sorted by size; A/B are report.json or run dirs |
| `timing.sh render [--explain] [--gantt] REPORT.json\|DIR...` | rebuild report.md from report.json |
| `timing.sh pull [--config F] -- CMD...` | Handheld plugin: parallel timed image pulls |

## Optional TOML (polish only)

Add `startup-timing.toml` (found in the cwd, `$STARTUP_TIMING_CONFIG`, or `--config`) only when you
want one of: display titles for groups (`[groups]`), service titles/descriptions shown with
`--explain` (`[services.NAME]`), a report location (`[report]`), or log-marker steps. Do not add it
just to name steps; the label already does that.

The log-marker plugin (Handheld-specific, `logmarkers.py`) loads only when the config has
`[[log_rules]]` or a service with `container`. It reads those containers' Docker logs after the run
and turns start/end markers into steps (rule keys: `service`, `pattern`, `action` = start / close /
skip / close_or_skip, `key`, `group`, `label`; `[ready_patterns]` closes open phases). Those rows
carry a dagger and nest under the tightest recorded step with the same `--service`. Copy
`handheld-multi-modal/startup-timing.toml` as the template. `--no-logs` disables it.

## Explaining a report to a human

Report order: headline (with two plain sentences naming the busiest service and what it did),
Time by service, then a collapsed list of all timed steps (`--explain` adds per-section totals).

1. State the headline: result (succeeded or FAILED with exit code), total time, the busiest service
   and its main work. Quote the sentence the report already gives.
2. Walk the Time by service table top to bottom. *Busy* is time with a step running; the strip shows
   when in the run; *Main work* names the biggest pieces. Shares can exceed 100% because services
   run in parallel, so say which ran at the same time.
3. Steps without `--service` land in *Shared setup and orchestration*. If that row is large, tag the
   steps with the service that owns them. The collapsed list shows time with no step running: a
   large value means untimed work, so suggest wrapping more steps.
4. Rows marked with a dagger in the step list are background service work derived from logs.
5. To compare: `timing.sh compare startup-timings/<named older run> startup-timings/latest` (the
   run's console output prints the exact command; do not infer age from a glob). A cold run against
   a warm one shows "now cached" / "was cached" and a note. When every step was cached, or none
   were timed, the report says so instead of showing idle time. Report the total
   delta then the largest per-step deltas, naming each step by its label.

### Cold versus warm

Headline "Run type" counts steps with status cached or reused (for example model cache hit, image
already present, container already running). "Cold" = nothing cached (first start; downloads and
builds dominate). "Partly warm" or "warm" = some or all skipped. Only compare cold with cold and
warm with warm; a warm run beating a cold one says nothing about a code change.

## Pitfalls

- Write labels for people ("Download detector model"), not code names or paths. Never put secrets,
  tokens, passwords or full command lines in a label or group; reports are shared.
- The helpers are no-ops outside `timing.sh run`, so scripts stay runnable alone; if no report
  rows appear, check the script ran under the wrapper (`STARTUP_TIMING_DIR` must be set).
- Parallel steps overlap, so section times can add up to more than the run. Use the Timeline
  and total, not the sum of rows. `--explain` shows wall time with overlaps merged.
- *Unavailable* means no duration was measured (step never ended, marker missing); it is not zero.
  A begun step without `timing_end` shows as unavailable or failed.
- `make -n` must not run the wrapper: use a literal `make` (not `$(MAKE)`) in the wrapper line, or
  guard with `ifeq ($(STARTUP_TIMING_DIR),)`.
- Do not edit `report.json` by hand; re-render with `timing.sh render`.

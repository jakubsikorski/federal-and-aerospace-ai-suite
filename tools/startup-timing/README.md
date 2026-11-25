<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Shared startup timing

This Python 3.11+ standard-library tool records command timings, reads configured service-log
markers, and creates Markdown and JSON reports. Application names, log patterns and report
labels belong in that application's `startup-timing.toml`, not in the shared runner.

To use it in another application:

1. Add a TOML file with the groups, service descriptions, report stages and labels used by that
   application. Add log rules only for stock-service phases with reliable start/end markers.
2. Add a small `startup_timing.py` entry point that loads
   `tools/startup-timing/startup_timing_core.py` and sets `STARTUP_TIMING_CONFIG` to the TOML
   path. Application scripts can import `begin`, `end` and `cached` from that entry point.
   Bash scripts and Make recipes can `source tools/startup-timing/timing.sh` instead:

   ```bash
   timed model_download 'Download model: a' ./download.sh a   # runs and times a command
   timing_cached backend_build 'Backend image matches sources' # records skipped work
   token=$(timing_begin setup 'TLS certificate'); ...; timing_end "$token"
   ```

   The helpers do nothing unless the command was started by `startup_timing_core.py run`.
3. Configure `report.parent_rules` for inclusive timings. Each child is stored with its own
   `id` and the enclosing step's `parent_id`; reports use those links to show nested work once.
4. Include `tools/startup-timing` when packaging the application separately.

Without further configuration, reports also show the slowest parent step and its largest parts,
the cache state of the `report.summaries` groups, a start offset for each step, image sizes, and
one combined row for successful steps shorter than `report.quick_step_seconds` (default 1).

The shared runner does not require Make; other applications can call the same `begin`, `end`
and `run` commands from their own startup scripts.

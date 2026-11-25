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
3. Configure `report.parent_rules` for inclusive timings. Each child is stored with its own
   `id` and the enclosing step's `parent_id`; reports use those links to show nested work once.
4. Include `tools/startup-timing` when packaging the application separately.

The shared runner does not require Make. A Makefile can keep only the small timing wrappers
needed at command boundaries; other applications can call the same `begin`, `end` and `run`
commands from their own startup scripts.

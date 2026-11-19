---
name: uav-customize-telemetry-overlay
description: >-
  Add, modify, or remove a field drawn in the on-stream telemetry overlay
  (ALT, SPD, HDG, LAT, LON, SATS, ...) of an already-deployed
  uav-vision-analytics stack, by editing the gvapython overlay script for
  the deployment mode in use (pymavlink or uavsdk) and restarting the
  affected pipeline. USE FOR: "add climb rate / battery / a new MAVLink
  field to the overlay", "show a new MQTT telemetry key from the SDK",
  "change how altitude/speed is formatted or labeled", "remove a line from
  the overlay", "the overlay shows all zeros or didn't update after my
  edit". DO NOT USE FOR: turning the whole overlay on/off for a camera
  (that's the `overlay:` field in configs/cameras-*.yaml — use
  `uav-manage-cameras`), the detection boxes themselves, or scaffolding a
  brand-new stack (use `uav-vision-analytics`).
license: Apache-2.0
compatibility: >-
  Requires an already-deployed uav-vision-analytics stack checked out at a
  known path. The two deployment modes use different overlay scripts and
  telemetry sources and are not interchangeable.
---

<!-- SPDX-FileCopyrightText: (C) 2026 Intel Corporation -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Customize UAV Telemetry Overlay Skill

Each output frame carries a small telemetry overlay drawn by a `gvapython`
element. This skill adds, modifies, or removes a field in that overlay for
an **already-deployed** stack, in the script matching its deployment mode.

| Mode | Script | Telemetry source |
| --- | --- | --- |
| pymavlink (standalone) | `gvapython/telemetry-overlay-pymavlink.py` | MAVLink UDP broadcast via `pymavlink` |
| uavsdk | `gvapython/telemetry-overlay-uavsdk.py` | MQTT topics `uav/{UAV_ID}/telemetry/#` |

## Canonical Documentation (read before acting — do not rely on memorized snippets)

| Task | Read this file in the target repo first |
| --- | --- |
| Overlay script structure, add/modify/remove a field, restart to apply | `docs/user-guide/how-to-customize/telemetry-overlay/customize-telemetry-overlay.md` |
| What a downstream metadata consumer expects if a field is removed entirely | `docs/user-guide/how-to-guides/view-detection-logs.md` |

Read the live copy of the target repo's overlay script(s) and this doc
together — the exact `latest_data` keys, MAVLink message field names, and
MQTT topic/key names are specific to the shipped script and can change
between app releases; do not hardcode a remembered field name or script
excerpt.

## How to Use This Skill

1. Confirm `{{STACK_DIR}}` and deployment mode (pymavlink or uavsdk) — the
   two overlay scripts are different files with different data sources.
2. Ask, in one batched question:
   - Add / modify / remove a field, and which one.
   - For "add": which telemetry value (a MAVLink message+field in pymavlink
     mode, or an MQTT topic+JSON key in uavsdk mode), its label and
     formatting (units, decimal places).
   - For "modify": the field to change and the new source/format.
   - For "remove": the field to remove, and whether to also stop tracking it
     upstream (vs. just hiding the display line) — warn if anything
     downstream (a JSONL metadata consumer) may depend on the key.
3. Read the target repo's current overlay script for the mode in scope, and
   the doc above, before editing — confirm the receiver thread
   (`MavlinkReceiver`/`MqttReceiver`) and `DrawDynamicText.process_frame()`
   structure matches what the doc describes; the shipped script is the
   ground truth if it has diverged.
4. Edit the script: update the receiver thread's parsing (if adding/
   modifying) and `process_frame()`'s `lines` list, keeping `latest_data`
   access under its lock in both places.
5. Restart the affected pipeline/stack and verify per the guardrails below.

## Execution Guardrails

- Edit only the overlay script for the deployment mode actually running —
  editing `telemetry-overlay-pymavlink.py` has no effect on a uavsdk stack
  and vice versa.
- Keep `latest_data` reads/writes inside the existing `lock` — the receiver
  thread and `process_frame()` run concurrently; an unlocked read can show a
  torn/half-updated value.
- Adding a field means touching three places consistently: the
  `latest_data` default, the receiver thread's parsing branch for the
  matching message/topic, and the `lines` list in `process_frame()` — an
  edit to only one of the three is a common cause of "no effect" or a
  `KeyError`.
- Removing a field: deleting only its `lines` entry is safe and reversible;
  also deleting the `latest_data` key stops it being tracked entirely —
  confirm with the user whether any downstream metadata consumer
  (`view-detection-logs.md`) depends on that key before doing so.
- `gvapython` scripts are volume-mounted, not baked into the image, but the
  **pipeline must be restarted** to reload them — editing the file alone
  does not change a running stream. State clearly which restart is needed
  (pipeline restart via disarm/arm vs. full `make pymav-down && make
  pymav-up && make start-rtsp`) based on what the target doc currently says.
- Do not change the overlay's rendering element (`gvawatermark` /
  `gvametaconvert`/`gvametapublish` wiring in the pipeline string) — that is
  a pipeline-definition concern, out of scope for this skill
  (see `uav-manage-pipelines`).

## Completion Criteria

1. The edited script has no syntax errors (`python3 -m py_compile` on the
   script, or watch the container logs for a `gvapython` load failure after
   restart).
2. The affected pipeline(s) restarted and are `RUNNING`
   (`make cameras-status` or the REST status endpoint).
3. `ffplay -rtsp_transport tcp rtsp://<HOST_IP>:8555/uav-<id>` shows the new/changed line in the
   overlay, or confirms a removed line is gone.
4. The field's value updates as telemetry changes (vehicle moves / SDK
   publishes new values) rather than staying frozen at a default.
5. No `KeyError` or overlay-related errors in the container logs after the
   restart.

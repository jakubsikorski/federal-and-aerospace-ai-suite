<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Add, Modify, or Remove a Telemetry Overlay Field

Each output frame carries a small telemetry overlay (`ALT`, `SPD`, `HDG`,
`LAT`, `LON`, `SATS`, ...) drawn by a `gvapython` element. This tutorial walks
through the overlay script's structure and shows how to add a new field
(e.g. from a different MAVLink message, or a new UAVSDK MQTT topic key),
modify an existing one, or remove one safely.

## Prerequisites

- Familiarity with the deployment mode you're editing — the two modes use
  **different** overlay scripts and data sources:

  | Mode | Script | Telemetry source |
  | --- | --- | --- |
  | Standalone (pymavlink) | `gvapython/telemetry-overlay-pymavlink.py` | MAVLink UDP broadcast (`:14541`) via `pymavlink` |
  | UAV Mission Compute SDK | `gvapython/telemetry-overlay-uavsdk.py` | MQTT topics `uav/{UAV_ID}/telemetry/#` |

- The stack running in the target mode, so you can restart it to pick up
  script changes (`gvapython` scripts are volume-mounted, not baked into the
  image, but the pipeline must be restarted to reload them).

## How the overlay script works

Both scripts share the same shape:

1. A background thread (`MavlinkReceiver` / `MqttReceiver`) continuously
   parses incoming telemetry messages and writes parsed values into a shared
   `latest_data` dict, guarded by a `lock`.
2. `DrawDynamicText.process_frame(frame)` — called by `gvapython` once per
   video frame — reads `latest_data` under the same lock, formats each value
   into a text line, and calls `frame.add_region(...)` per line. `gvawatermark`
   (or `gvametaconvert`/`gvametapublish`, depending on the pipeline) renders
   each region's label text onto the frame.

```python
# telemetry-overlay-pymavlink.py (excerpt)
latest_data = {
    "altitude": 0.0, "speed": 0.0, "heading": 0.0,
    "latitude": 0.0, "longitude": 0.0, "gps_altitude": 0.0,
    "gps_fix": 0, "satellites": 0,
}
# ... MavlinkReceiver.run() parses GLOBAL_POSITION_INT / VFR_HUD / GPS_RAW_INT
# ... DrawDynamicText.process_frame() builds `lines` from latest_data and
#     calls frame.add_region(10, y, 1, 1, line) for each
```

## Steps

### Add a new field

**Example: add vertical climb rate (`VFR_HUD.climb`) in pymavlink mode.**

1. Add a key to `latest_data`:

   ```python
   latest_data = {
       ...,
       "satellites": 0,
       "climb_rate": 0.0,   # new
   }
   ```

2. In `MavlinkReceiver.run()`, populate it from the matching MAVLink message
   field (already-parsed messages are handled in the `elif msg_type == ...`
   chain):

   ```python
   elif msg_type == "VFR_HUD":
       latest_data["speed"] = msg.groundspeed
       latest_data["climb_rate"] = msg.climb   # new
   ```

   (For UAVSDK mode, add the equivalent inside `_on_message()`, keyed off the
   relevant MQTT topic's JSON payload — e.g. `data.get("down_m_s", 0.0)` under
   the existing `velocity` topic handler.)

3. In `DrawDynamicText.process_frame()`, read it out under the lock and add
   one line to `lines`:

   ```python
   climb_rate = latest_data["climb_rate"]
   lines = [
       ...,
       f"SATS  : {satellites}",
       f"CLB   : {climb_rate:.1f} m/s",   # new
   ]
   ```

4. Restart the affected pipeline (or the whole stack) so `gvapython` reloads
   the script:

   ```bash
   make pymav-down && make pymav-up && make start-rtsp
   ```

### Modify an existing field

Change only the formatting or source line — e.g. switching `ALT` from
relative to absolute altitude:

```python
# was:
latest_data["altitude"] = msg.relative_alt / 1000.0
# now:
latest_data["altitude"] = msg.alt / 1000.0   # GLOBAL_POSITION_INT.alt (AMSL)
```

No changes to `latest_data`'s keys or `process_frame()` are needed for a
same-key change like this.

### Remove a field safely

1. Delete the corresponding line from `lines` in `process_frame()`.
2. Leave the key in `latest_data` and the receiver thread's parsing alone
   **unless** you also want to stop tracking it — removing only the display
   line has no side effects.
3. If you remove the key from `latest_data` entirely, check whether anything
   downstream reads it — the overlay ROI labels are display-only, but if a
   JSONL metadata consumer (see
   [View Detection Logs](../../how-to-guides/view-detection-logs.md)) or another script
   expects that field to exist, removing it upstream will break that
   consumer instead of just the overlay.

## Verify

```bash
ffplay rtsp://<HOST_IP>:8555/uav-front-camera
```

Confirm the new/changed/removed line appears (or doesn't) in the upper-left
overlay, and that its value updates as the simulated UAV moves.

## Troubleshooting

An edit with no effect, a `KeyError` in `process_frame()`, and a field that
stays at its default value are covered in
[Troubleshooting — Overlay script edit has no effect](../../how-to-guides/troubleshooting.md#overlay-script-edit-has-no-effect)
and [No telemetry overlay on stream](../../how-to-guides/troubleshooting.md#no-telemetry-overlay-on-stream-all-zeros).

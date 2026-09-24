# Operator guide

Open `https://<device>:8443` and accept the self-signed certificate.

![Operator UI](images/operator-ui.jpg)

## Top bar

| Control | What it does |
|---|---|
| **Camera 1 / Camera 2 …** | Switches the live video to that camera. |
| **Mission** | Opens the mission panel. Upload a PDF or DOCX, ask questions about it, **Re-ingest** or **Clear mission**. |
| **Rules** | Opens the alert rules. Pick object type, camera, minimum confidence and speech cooldown, then **Add rule**. Existing rules have **Edit** and **Delete**. |
| **Audio off / Audio on** | Tap once to allow the browser to play spoken alerts. Stays off until you tap it. |
| Clock / connection | Local time and whether the UI is connected to the Backend. |

## Model strip

**Vision**, **LLM**, **Transcription** and **Speech** show each model's status
and device. Click one to see model, configured device, the device the
service reports and host; click again or **Close** to hide.

## Live camera (top left)

- Annotated video from the selected camera: boxes and track IDs.
- **Fullscreen** enlarges the video.
- The line under the video shows what is being spoken
  ("Speaking: …", "Audio off · …").

## Alerts (under the video)

Each alert is one incident: object, camera, first and last sighting, state.

| Button | What it does |
|---|---|
| **View** | Shows the tracks that raised the alert in the Evidence panel. **Back to live evidence** returns. |
| **Acknowledge** | Marks it seen. A new sighting reopens it. |
| **More… → Snooze 5 / 15 min** | Silences it for that time. |
| **More… → Dismiss / False alarm** | Closes it. |
| **More… → Retry delivery** | Resends an alert that failed to dispatch. |
| **More… → Disable rule** | Stops that rule on every camera. |
| **Speak** | Test: speaks the alert now (needs Audio on). |
| **Remove** | Deletes the alert from the list. |

The speech label tells you whether it was **spoken**, **not played (Audio
off)**, **not spoken (cooldown)** or **text only (speech unavailable)**.

## Evidence (right)

Detected objects with their image crops.

| Control | What it does |
|---|---|
| **View: Current** | Objects on screen now. |
| **View: History · priority / newest** | Recorded tracks, priority objects first or newest first. |
| **Show object type** | Filters by class (person, car …). |
| **Evidence: All records / Has image** | Hides records without a crop. |
| **Pause / Resume** | Freezes the list so it stops moving; **Updates available** loads what arrived. |
| Click a record | Opens its images and details; **Back** closes. |
| **Ask** | Opens the assistant (below). |

## Ask (assistant)

Type a question and press **Search**.

- **Answer mode: Ask anything** answers about camera history, the mission or
  general topics; **General chat** skips camera history.
- **Camera scope** and **History filters** (object type, time range) narrow
  camera-history questions.
- Shortcuts: **Mission objective** (from the loaded mission), **Explain a
  topic** (general chat).
- **Previous / Next** step through answers; **Back** closes.

General chat remembers the last few exchanges; camera and mission answers
do not. **New conversation** forgets them.

## Resources (bottom left)

CPU, Memory, GPU engines, NPU and Package power tiles. Click a tile to chart
it; **Expand** enlarges the chart; **Details** lists raw readings.

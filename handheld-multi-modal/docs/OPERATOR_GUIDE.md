# Operator guide

Open `https://<device>:8443` and accept the self-signed certificate.
Red numbers in the pictures match the numbered lists below.

![Operator UI](images/operator-ui.jpg)

## Top bar

![Top bar](images/guide-top-bar.jpg)

1. **Camera tabs** — pick the live camera. The last camera you used is
   restored on the next visit. **All cameras** shows the live grid (below).
   A yellow badge counts open alerts. With many cameras the tab strip scrolls
   sideways; a fade on the right means more tabs.
2. **Mission** — mission documents and brief (see [Mission](#mission)).
3. **Manage alerts** — alert rules (see [Rules](#rules)).
4. **Audio off / Audio on** — tap once so the browser may play spoken
   alerts. Speech is silent until you do.
5. **Clock** — local time; "live updates" means the UI is connected.
   **Connection lost · retrying** means data may be stale until it reconnects.
6. **Models & devices** — each model with its device and status. Click one
   for details.

![Model details](images/guide-model-details.jpg)

1. Clicked model (click again to close).
2. Status, model, device and host. **Close** hides it.

## All cameras

![Camera grid](images/guide-camera-grid.jpg)

1. **Tile** — one camera, up to four per page. Tap to open that camera.
   Tiles with open alerts glow.
2. **‹ / ›** — next or previous four cameras.

## Live camera and alerts

![Live camera and alerts](images/guide-video-alerts.jpg)

New alerts update camera badges, highlights and a notification. Tap **Alerts**
to open the list. Incoming alerts do not automatically expand it or resize video.

1. Annotated video of the selected camera (boxes on detected objects). The
   small badge shows estimated video delay when available.
2. **Fullscreen** — enlarge the video.
3. **Alert** — object, camera, sightings, first–last time, state. Tap the
   picture or text to show the objects that raised it in Evidence.
4. **Acknowledge** — "I've seen it". First button, amber border. A new
   sighting reopens the alert.
5. **View** — same as tapping the alert.
6. **Speak** — say the alert now (needs Audio on). A soft chime plays first.
7. **More…** — Snooze 5 / 15 min, Dismiss, False alarm, Retry delivery,
   Disable rule, **Remove from list** (asks to confirm).

Speech labels: **spoken**, **not played (Audio off)**, **not spoken
(cooldown)**, **text only (speech unavailable)**.

After **View** the Evidence panel shows only that alert:

![Alert view](images/guide-alert-view.jpg)

1. **Back to live evidence** — return to the normal list.
2. Each object that matched the alert; click for images.

## Evidence

![Evidence](images/guide-evidence.jpg)

1. **View** — **Current** (on screen now), **History · priority** or
   **History · newest** (recorded tracks).
2. **Show object type** — only persons, cars, …
3. **Evidence** — **All records** or **Has image**.
4. **Person only** — show only priority objects.
5. **Compact / Cards** — switch between dense rows and image cards; the choice
   is remembered.
6. **Pause / Resume** — freeze the list; **Updates available** loads what
   arrived meanwhile.
7. **Object card / row** — click to open its images.
8. **Ask** — open the assistant.

A **While you were away** card appears after returning from five or more
minutes away. It summarizes at least the new alerts and track counts visible
through the existing history APIs; tap **View** to jump into them.

![Evidence details](images/guide-drawer.jpg)

1. **Back** — close the details.
2. Stored image, camera, times and confidence; thumbnails switch images.
   **Play** (shown with two or more images) cycles them like a flip book.
3. **Ask about this object** — opens Ask with a question prefilled; you send it.

## Ask (assistant)

![Assistant](images/guide-ask.jpg)

1. Type a question.
2. **Send**.
3. **Camera scope** — which camera's records; **Use Camera N** picks the one
   on screen.
4. **Answer mode** — **Ask anything** (records, mission or general) or
   **General chat** (no camera records).
5. **History filters** — object type and time range.
6. **Find person** — recorded priority objects, last 10 minutes.
7. **Mission objective** — from the loaded mission.
8. **Explain a topic** — general conversation.
9. **New conversation** — forget earlier questions.
10. **Back** — close the assistant.

General chat remembers the last few exchanges; camera and mission answers do
not. General answers never describe what is on camera.

## Mission

![Mission](images/guide-mission.jpg)

1. **Browse… / Upload** — add a PDF or DOCX mission document.
2. **Delete** — remove that document.
3. **Re-ingest** — reload the documents and rebuild the brief.
4. **Clear mission** — remove all mission documents (camera history stays).
5. **Ask** — question about the mission documents only.
6. **Brief** — objective, times, locations, units, restrictions and
   resources extracted from the documents.

## Rules

![Rules](images/guide-rules.jpg)

1. Open **Manage alerts**. Choose **Appears** or **Stays too long**.
2. Check **Camera** (defaults to the camera being viewed) and **Watch for**.
   **Anything detected** includes all object types detected by the configured pipeline.
3. For **Stays too long**, choose 10 seconds, 30 seconds, 1 minute or a custom duration.
4. **Area** defaults to the whole camera view. Choose **Draw an area** to limit it.
5. **More options** contains confidence, active hours and speech settings.
6. Read the plain-language preview, then **Create alert**.
7. Existing alerts show their conditions. **Edit**, **Turn off / Turn on** or **Delete**
   are available beside each. Editing finishes with **Save changes**.

Switching to **Appears** removes the dwell requirement. **Remove area** returns to
whole-view monitoring after saving. Each object's dwell timer is independent;
different objects never contribute to a shared timer.

A rule fires once per tracked object, not once per frame.
Dwell requires continuing qualifying observations; missing zone geometry or a
long observation gap restarts the timer. Changing matching conditions or turning
a rule off and back on also restarts pending dwell. Editing a scheduled rule
preserves its displayed timezone, even from a device in another timezone.

### Drawing a zone

![Draw zone](images/guide-zone.jpg)

Full-screen frozen frame of the camera. Outline the ground where people or
vehicles walk; an object counts by where its feet touch the ground.

1. **Corners** — 3 to 5. Turns amber at 5.
2. **Next step** — what to do now. A sixth tap shows an amber warning.
3. **Corner** — tap the picture to add, drag to move.
4. **Cancel** — leave without changes. Closing Rules also cancels.
5. **Undo point** — remove the last corner.
6. **Clear all** — start again.
7. **Use this area** — enabled from 3 corners; then tap **Create alert** or **Save changes**.
   To remove a zone, tap **Remove area** in the alert form, then **Save changes**.

## Resources

![Resources](images/guide-resources.jpg)

1. **Tiles** — CPU, Memory, GPU engines, NPU and Package power. Click one to
   chart it.
2. **Chart** — last 5 minutes of the selected tile.
3. **Expand** — larger chart.
4. **Details** — raw readings.


## Voice questions and spoken answers

Tap **🎙 Talk** in the top bar from any console view. It opens Assistant and
starts recording after microphone permission is granted. Tap **Send question**
when finished; holding the button is not required.
Use trusted HTTPS. **Listening** means the browser microphone is active. Capture
ends when you tap Send question, after 15 seconds, or after a pause following speech. **Cancel**
or Escape discards the voice request. The microphone closes when capture ends.

**Transcribing** is followed by the recognized question and the normal answer
progress. Voice uses the same camera scope and answer mode as typing. If capture
or transcription fails, type your question; no camera settings are changed.

With **Audio on**, answers play as short sentences (long sentences split into
bounded clauses). The complete text stays visible. **Stop speaking** cancels the
remaining speech without deleting the answer. Tapping Talk interrupts
playback immediately. Audio off, synthesis errors or browser playback restrictions
leave the text available. Tablet microphone and autoplay behavior still require
verification on the target browser; see Phase 9 evidence.

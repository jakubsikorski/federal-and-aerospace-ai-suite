# Handheld ops — operator guide

This application is a local edge camera-evidence console. The operator watches live
annotated video, inspects recorded object tracks and cropped evidence, searches
camera history, reads mission documents and monitors computing resources.

## Cameras and evidence
Choose Camera 1, Camera 2 or another configured camera in the top bar for live
video. All cameras gives an evidence overview; choose one camera to watch video.
Fullscreen enlarges video. Lost video connections retry automatically; the video
panel shows reconnection status. Switching cameras cancels the previous retry.
History evidence shows 50 tracks per page; Older and Newer below the list page through the rest.
Open an evidence card for its image and track details. Crops are selected snapshots,
not full recordings. One track is not necessarily one unique person. Tracking IDs
cannot identify people or link identities across cameras. Missing detections or
images do not prove absence. Classification fields depend on the pipeline metadata. The Assistant discovers
named categorical labels recorded for each camera/session and can filter those
labels in a time window. Missing fields are unknown, not a negative classification.
Example values are observed examples, not the classifier’s complete taxonomy.
Opaque field names may require clarification; do not invent their meaning. Feed gaps, occlusion and stale data limit conclusions.
Search covers retained records in each camera's current tracking session, not all
past sessions. A pipeline restart starts a new tracking session.

## Adding and removing cameras
The Cameras button in the top bar, right after the camera tabs, opens Manage cameras.
It adds, renames, turns off or removes a camera while the system runs. There is no
fixed camera limit; each camera that is on adds processor load. Each row shows the camera state (Live, Starting, Off)
with View and Edit. A new camera is either a publisher on this device or a network
camera by rtsp:// address on an allowed network; addresses with a user name or
password are refused and belong in MediaMTX. After adding a publisher camera, a card
shows the rtsp://127.0.0.1:8554/camN address to send H.264 video to, with Copy and an
example ffmpeg command. The address works only on this device and is also shown under
Edit. The card changes to Live when video arrives. Turning off or removing a camera
stops its video and detection but keeps its history and alerts. Deployment cameras
can only be renamed. Alert rules on a removed camera show "camera off or removed" and
can only be turned off or deleted. The assistant
cannot add or remove cameras.

## Assistant controls
Open Assistant beside the live video. On short screens it opens a focused chat
view: camera status stays in the top bar, and Back to camera returns to video.
The Find shortcut searches the configured priority object type (person by default)
in the last ten minutes. History offers priority-first or newest-first ordering. Camera scope is independent of the video selection:
choose a camera or All cameras, or press Use Camera N to adopt the on-screen camera.
History filters select object type and time when the question is empty. For a typed
search, name the object and time window: "Show person tracks in the last ten minutes."
Compare Camera N repeats a result's exact time window on another camera.
Ask anything routes questions to records, mission documents or conversation.
General chat answers explanations without searching camera records. It can also
explain this application using this guide. The model cannot see live video.
Explain simply and Give an example continue the answer. Copy answer copies its
text. Enter sends; Shift+Enter inserts a line. Latest reply returns to the newest
answer after reading older messages. New conversation clears chat context, not
camera records or mission documents. Conversation context is short and temporary,
not durable memory. Backend retains at most four recent general exchanges for ten
minutes of inactivity; changing between camera history and conversation resets it.

## Mission and Mission Brief
Open Mission in the top bar. Upload a PDF or Word (.docx) file, one at a time; the
panel shows the size and page limits. Only text is read: images and scanned pages are
not, and a file with no readable text is refused.
The application retains originals and indexes them through stock Chat Q&A.
Mission Brief summarizes objective, times, locations, people/units, restrictions,
resources, dependencies and risks from uploaded documents. Not specified means
that the documents did not establish that field. It is not a live situation report,
and does not incorporate current detections. It regenerates after confirmed document
changes. A stale brief may describe an older document set; check its status.
Reingesting means retained documents are being restored to the retrieval service;
mission answers wait until ready. Re-ingest explicitly retries ingestion or brief
failure. Filenames list available sources, not verified answer-level citations.
Delete removes the named mission source; Clear mission removes mission documents
and originals, not camera records. Ask about objectives or restrictions to consult
mission material. Application capabilities do not establish mission facts.

## Resources, services and limitations
Resources shows CPU, memory, GPU/NPU and power telemetry when available. Details
shows component readings; Expand enlarges the chart. Stale or unavailable readings
are not zero. Models & devices shows model, device and host information; configured
placement is different from service-reported execution. Starting means a model
service has not answered since the application started; it may still be loading or
downloading its model (the first start can take up to an hour). This guide cannot establish
current health, resource readings or what a camera is observing.
DL Streamer detects/tracks objects and creates crops; MediaMTX carries video directly
to the browser. Backend stores observations and serves evidence and queries; it does
not analyze video with the language model. Stock Chat Q&A provides language inference
and mission retrieval. Speech services support spoken interaction and alert announcements. Chat cannot restart services,
clear records, change rules or execute operator actions. Unknown camera coverage,
locations, model versions and hardware details must not be invented.


## Pause control
Pause freezes updates to the evidence list so you can inspect it. Live video and
camera processing continue. Press Resume to resume list updates. It does not pause
video playback or stop collection of observations.

## Brief generation
Mission Brief is generated by stock Chat Q&A from the uploaded PDF/DOCX mission
documents. It summarizes objective, times, locations, people/units, restrictions,
resources, dependencies and risks. Missing information is shown as Not specified.
The brief regenerates after confirmed document changes. It does not watch cameras
or describe live conditions. Open Mission to see its sources and freshness; use
Re-ingest to retry a failed generation.

## Search camera scope
The camera shown in live video and the camera selected for Assistant searches are
independent. Use Camera scope to choose one camera or All cameras. Use Camera N
adopts the on-screen camera explicitly. Compare Camera N searches another camera
over the exact time window of the current result.

## New conversation control
New conversation clears the visible chat and starts a fresh conversation context.
It does not delete camera history, evidence images, or mission documents. The
assistant only retains a short, temporary conversation context, not permanent memory.

## Closest answer, never a refusal
The Assistant always answers with its closest search and states what it assumed
first: no time given means the last 10 minutes; a camera named in the question
overrides Camera scope; details the detections cannot check are named, and all
matching tracks for that time are shown so the operator can check the images.
Questions without camera, object or time words, including "What model is this?",
are answered as general conversation from the Models & devices facts.

## Video understanding
The language model cannot see or watch live camera video. DL Streamer detects and
tracks objects; the Assistant searches those recorded observations and evidence
images. General chat explains topics and application controls, not live scenes.


## Broader results
When requested fields are unavailable, Show broader results offers the same object
types, cameras and exact time window while dropping only the unavailable attribute
conditions. The excluded conditions are shown before selection. If an exact search
has no matches, the option removes its attribute filters while keeping object type,
cameras and time. These are broader records for inspection, not verified matches to
the excluded conditions or a visual-similarity ranking.

## GPU engine summary
The GPU tile shows the highest current engine usage and its engine name, plus the
number of engines. Select it to see every engine. Partial means some readings are
not current; Stale readings are historical, and Unavailable is not zero usage.


## Manage alerts
Open Manage alerts. Choose Appears or Stays too long, check the camera and Watch for.
Anything detected matches any object type detected by the configured pipeline.
Stays too long offers 10 seconds, 30 seconds, 1 minute or a custom duration.
Each tracked object has its own dwell timer; times from different objects never add up.
Area defaults to the whole view. Draw an area restricts matching to a polygon on one
camera; after three corners a new corner joins the nearest edge, and an outline that
crosses itself cannot be saved; Remove area restores the whole view after saving. Any
camera can be chosen; the area is drawn on that camera's latest frame. More options contains
confidence, Announce again after, active hours and speech settings. Read the preview and Create alert.
A new alert shows a banner over the video until it is viewed or acknowledged, and its area
pulses amber on the video and on the All cameras tile.
Existing rules can be edited, turned off/on or deleted. Save changes completes edits.
Switch to Appears to remove dwell. Missing qualifying observations can reset dwell;
tracking does not establish a person's identity across cameras. Chat explains these
controls but cannot change alert rules for the operator.


## Talk to the assistant
Tap Talk in the persistent top bar to open Assistant and start recording.
Allow microphone access on first use. Tap Send question to finish; no hold is required. Capture happens only in the browser over trusted HTTPS. Listening
indicates an active microphone; Send question, 15 seconds or a pause after speech ends
capture and closes the microphone. Cancel or Escape discards capture/transcription; a cancelled question never speaks, and queued alert announcements are kept.
The recognized question uses the same camera scope, answer mode, conversation
and routing as typing. Transcribing and answering have visible progress. If the
microphone or transcription is unavailable, type the question instead.
Audio on enables spoken replies. Answers play in short sequential sentences or
clauses, with the full text visible. Turning Audio off cancels the current speech and
its remaining chunks; Talk immediately interrupts playback. Speech already
being synthesized may finish on the service, but cancelled audio is not played.
With audio on, "Checking." plays as soon as a spoken question is recognized, and the
answer follows within a few seconds. Your answer is spoken before alert announcements: an alert never interrupts it and
is announced right after. Alerts still appear on screen immediately.

Voice recovery controls: **Cancel question** abandons a running question and stops
its answer speech; the stock inference service may still finish its in-flight work,
so the next answer can be slower.
Turning **Audio off** silences speech while leaving the text request running. Finish or
cancel a running question before starting another with Talk. **Edit and resend**
restores the recognized question and its original camera scope for correction.
**Replay answer** reads the completed answer again without asking the model again;
if Audio is off, enable it first. Failed playback remains visible. Interrupted text
stays on screen as incomplete, with **Retry question** available. While recording, a
hint shows the 1.5-second silence auto-send and 15-second limit. Replay reads about
the first 1,500 characters and is refused while an answer is still in progress.

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
stops its video and detection but keeps its history and alerts. Deployment (demo)
cameras can be renamed and turned off or on, and stay off across restarts until
turned on again; they are removed only in the deployment configuration. Alert rules on a removed camera show "camera off or removed" and
can only be turned off or deleted. The assistant
cannot add or remove cameras.

## Assistant controls
Open Assistant beside the live video. On short screens it opens a focused chat
view: camera status stays in the top bar, and Back to camera returns to video.
The Find shortcut searches the configured priority object type (person by default)
in the last ten minutes. History offers priority-first or newest-first ordering. Camera scope follows the video selection:
the camera on screen, or All cameras on the grid. You can pick another until you switch cameras.
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

## How this application is built
- Purpose: an operator console on a handheld device. It shows live camera video with detected objects boxed, keeps detections and object images as searchable evidence, raises alerts from operator rules, and answers typed or spoken questions about camera records, mission documents and general topics.
- Device: one Intel Core Ultra edge device (CPU, integrated Arc GPU, AI Boost NPU). No cloud, no add-on hardware.
- Custom code: only the Backend (Python, FastAPI). Everything else is stock and unmodified, runs in its own container, and is used only through published APIs.
- Open Edge Platform release-2026.2.0 services (exactly six): DL Streamer Pipeline Server, Alert Agent, Chat Q&A Core, Audio Analyzer, Text To Speech, Metrics Manager. Model Download is used only during setup.
- Open-source, not Open Edge Platform: MediaMTX (video relay), Coturn (loopback-only WebRTC relay), Mosquitto (MQTT broker), nginx (HTTPS, localhost only).
- Video, in four steps: 1. Each camera sends RTSP video to MediaMTX. 2. DL Streamer Pipeline Server reads it from MediaMTX over RTSP, decodes it on the GPU, detects objects with YOLO26s INT8 on the NPU, tracks them with gvatrack on the CPU and draws boxes. 3. It sends the boxed video back to MediaMTX over WebRTC (WHIP). 4. The browser plays it from MediaMTX over WebRTC (WHEP). The Backend never touches video and nothing records it.
- Detections: DL Streamer Pipeline Server publishes each detection over MQTT through Mosquitto; the Backend receives it and stores it in SQLite. The Pipeline Server also saves cropped object images to a shared folder, and the Backend shows them as evidence.
- Backend: starts and stops camera pipelines, stores detections, checks alert rules, answers questions, forwards voice audio and speech, and relays resource metrics to the browser. It never touches video. Its calls: Pipeline Server POST/GET/DELETE /pipelines (start/stop per camera); Alert Agent POST /api/v1/actions/execute; Chat Q&A POST /chat and /documents; Audio Analyzer POST /v1/audio/transcriptions; Text To Speech POST /v1/audio/speech; Metrics Manager GET /api/v1/metrics/latest.
- Alerts, in three steps: 1. The Backend checks each detection against the operator's alert rules. 2. For each match it calls Alert Agent, which dispatches the alert and drops duplicates; no language model is involved. 3. The Backend shows the alert and speaks it through Text To Speech. Alert Agent never sees detections or rules.
- Assistant: Chat Q&A Core runs the language model Intel/neural-chat-7b-v3-3 on the GPU, with granite-embedding-125m-english and ms-marco-electra-base on the CPU for mission-document retrieval. It keeps no memory; the Backend sends recent turns.
- Camera history (what was seen): the Backend answers with a read-only SQLite search of stored detections and shows the matching tracks with their images. The language model never sees video or images.
- Voice: the browser records the microphone and sends the audio to the Backend, which has Audio Analyzer transcribe it with whisper-medium on the GPU. The text is then answered like a typed question. Answers and alerts are spoken by Text To Speech with SpeechT5 on the CPU and played in the browser.
- Storage, all on the device: SQLite holds detections and tracks (kept 7 days), rules, alert incidents, the mission document list and the Mission Brief. Chat Q&A Core holds the indexed mission documents. Cropped images sit on disk.
- The Architecture page shows the diagram, every API and step-by-step flows.
## Resources, services and limitations
Resources shows CPU, memory, GPU/NPU and power telemetry when available, plus a
Services view with a card per application service (share of total CPU, memory
used; collapsed shows the busiest plus "+N more · Expand"); the Memory tile shows free RAM. Details
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


## How detection works
DL Streamer Pipeline Server decodes each camera and runs the YOLO26s detector, an
OpenVINO INT8 model, on the Intel NPU by default with Intel hardware video decode;
fixed GPU and CPU variants exist. Detections below 0.75 confidence are dropped. The gvatrack
tracker follows each object across frames on the CPU and gives it a track ID.
Detections go to the Backend over MQTT, object crops are saved as
evidence, and the annotated video goes to the browser over WebRTC through MediaMTX.
The detector reports only its object classes with a box and confidence. It does not
identify people, read colours, plates or clothing, or judge what someone is doing.
See the Architecture page for every service.

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
Typed and voice questions search the camera shown in live video, or All cameras
when the grid is shown. Camera scope shows this choice; picking another camera there
applies until the next camera switch, and Use Camera N returns to the on-screen camera.
A camera named in the question overrides it. Compare Camera N searches another camera
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
A new alert shows a banner over the video until it is viewed or acknowledged. While the
alerted object is on camera now it is red and pulses ("In view now"); once the object has
left, an unacknowledged alert stays steady amber ("Needs attention", with last-seen time).
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

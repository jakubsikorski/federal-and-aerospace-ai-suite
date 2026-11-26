# Release Notes: UAV Ground Station

## Version 2026.3.0

**Release Date**: TBD

**New**:

- **Remote viewer for UAV Vision Analytics.** The dashboard runs on a separate machine, such as a
  ground-station laptop, and talks to the UAV host only after you enter its IP address and press **Connect**.
- **H.264 relay for live video.** A MediaMTX sidecar pulls each camera's H.264 RTSP stream from the UAV host on
  demand and serves it to the browser over WebRTC (WHEP) without transcoding. Cards recover on their own when
  a feed drops and comes back.
- **Camera cards** show the detection and VLM devices, the models, the prompt, and the pipeline state for each
  running pipeline, together with the scene captions published on `uav/caption/<camera>`.
- **Host utilization** of the UAV host (through its Metrics Manager) and of the ground-station machine.
- **Multiple UAVs in one session**, with a tab per host. Streams are registered under host-qualified names, so
  two UAVs can use the same camera name.
- **Live Natural Language Search** (`make up-vss`, optional): every running camera is indexed live with the
  Video Search and Summarization (VSS) stack, and the **Search Videos** popup returns ranked clips with
  thumbnails, SD and HD playback, and UAV and Camera filters. `make up-vss DEVICE=CPU|GPU|NPU|all` selects the
  device of the VSS models.
- **Flight Summary** report, built from the MQTT scene captions without VSS or an LLM, with a PDF download.
- **Architecture popup** that shows the services of the UAV host, the ground station, and VSS, the flow of
  video, captions, indexing and search, and the models on each device.
- **Simpler setup**: `make init` creates `.env`, detects the host IP from the default route and the GPU and NPU
  devices, generates the VSS credentials, and asks before downloading the VSS models. `make up` and
  `make up-vss` move the published ports to free ones and avoid the ports of the UAV Mission Compute SDK.
- **Optional TLS verification** of the UAV host's certificate (`VIZ_REMOTE_TLS_VERIFY`).
- **Documentation and agent skills** for deployment, configuration, and the backend routes.

**Known issues and limitations**:

- The dashboard is a viewer for a trusted network: it has no authentication, and it does not verify the UAV
  host's self-signed certificate unless `VIZ_REMOTE_TLS_VERIFY=true`.
- It makes read-only requests and never starts or stops pipelines on the UAV host.
- Video requires an H.264 RTSP stream from the UAV host. Otherwise the card shows a placeholder.
- If the cameras on the UAV host are stopped and started again immediately, a card can stay without video
  until the pipeline server on the UAV host is restarted (`docker restart uav-va-dlstreamer-pipeline-server`).
  Captions keep working.
- **Search Videos** is available only after `make up-vss`, and the first start downloads the VSS models into
  Docker volumes. Footage indexed from UAVs that are no longer connected stays searchable until
  `make clean-vss-index`.
- The VSS images use the `2026.3.0` tag.
- The ground station and the UAV Mission Compute SDK use overlapping ports. Install the SDK before the ground
  station on the same machine, or set different ports in `.env`.

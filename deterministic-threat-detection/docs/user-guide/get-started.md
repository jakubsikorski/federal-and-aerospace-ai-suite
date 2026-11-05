# Get Started

Set up four machines and a Moxa TSN switch, then start both use cases with one command.

## Prerequisites

- Four machines and a Moxa TSN-G5000 switch as listed in [System requirements](./system-requirements.md).
- Machine-1 is the machine you work on. It has `git`, `make` and Docker, and passwordless ssh
  works from it to machines 2, 3 and 4 (`ssh-copy-id <user>@<machine>`). Your user is in the
  `docker` group on all four. [First-time setup](./first-time-setup.md) walks through these.

## Clone the project

```bash
git clone https://github.com/open-edge-platform/federal-and-aerospace-ai-suite.git
cd federal-and-aerospace-ai-suite/deterministic-threat-detection
```

Clone it on machine-1 only: `make up` copies what the other machines need. The one-time setup scripts are
the exception; [First-time setup](./first-time-setup.md) copies them by hand.

## First time setup guide

One-time steps per machine and for the switch (ssh and Docker, NIC and VLANs, PTP, switch,
`lab.env`): **[First-time setup](./first-time-setup.md)**. Skip it once done.

## Architecture diagram

![Architecture: live traffic, no load](./_assets/architecture-video-idle.gif)

```mermaid
flowchart LR
    subgraph C["machine-2 / machine-3"]
        V["test video<br/>RTSP"]
        I["DL Streamer<br/>inference"]
    end
    subgraph M["machine-1"]
        B["MQTT broker"]
        D["dashboard<br/>latency"]
        R["mediamtx relay"]
        S["DL Streamer +<br/>SceneScape"]
        H["live HOTA"]
    end
    N["machine-4<br/>load, VLAN 5"]
    V --> I -->|"use case 1: MQTT, VLAN 1"| B --> D
    V -->|"use case 2: RTSP, VLAN 1"| R --> S --> H --> D
    N ==>|"load, VLAN 5"| SW["Moxa switch<br/>Qbv"]
    SW -.->|"confined to its slot"| M
```

- **machine-2 / machine-3:** replay a test video as an RTSP camera, and run the use case 1
  inference pipeline, which publishes results over MQTT.
- **machine-1:** receives MQTT (use case 1), pulls both RTSP streams once, decodes and tracks them
  with DL Streamer and SceneScape (use case 2), and serves the dashboard.
- **machine-4:** generates the background load on VLAN 5.
- **Switch:** PTP grandmaster; with Qbv on, VLAN 5 is confined to its own time slot.
- **Lab network (grey):** ssh, load control and switch commands. Never shaped.

The same diagram is live in the dashboard (**Architecture** button).

## Application Deployment

```bash
make up
```

`make up` starts the test-video streams, the dashboard, the load agent, the camera pipelines and
SceneScape.

- The first run pulls the Docker images (several GB; the DL Streamer image alone is about 5 GB)
  and downloads the detection model, so it takes several minutes.
- Before the first model download, `make up` asks you to accept the model license (Open Model
  Zoo, Apache-2.0). Answering anything other than yes aborts the setup. The answer is remembered
  in `src/data/models/.terms-accepted`. For non-interactive runs, set `ACCEPT_MODEL_TERMS=yes`.

When it finishes it prints:

- Dashboard: `https://<machine-1 lab address>/dashboard/`
- SceneScape: `https://<machine-1 lab address>/` (user `admin`, password in
  `src/scene/secrets/supass`)

The certificate is self-signed; accept the browser warning.

Try it:

1. **Use case 1** shows latency per camera (about 2 ms idle).

   ![Use case 1: latency](./_assets/uc1-latency.gif)

2. **Use case 2** shows the live tracking quality (HOTA) and the frames the tracker sees. The
   dip to about 0 once per minute is the 62 s test video restarting.

   ![Use case 2: tracking](./_assets/uc2-tracking-video.gif)

3. Switch off Qbv (the toggle works only after you set `SWITCH_ALLOW_WRITE=1`, see
   [First-time setup](./first-time-setup.md#6-lab-configuration-machine-1)) and move **Network load**
   to 1000 Mbit/s: latency climbs from milliseconds to
   seconds and the tracker rate (use case 2) and HOTA collapse. The Architecture view shows the
   streams stalling and frames lost.

   ![Load with Qbv off](./_assets/architecture-video-load-qbv-off.gif)

4. Switch Qbv on: the load is confined to its slot (about 95 Mbit/s gets through) and both
   recover.

   ![Load with Qbv on](./_assets/architecture-video-load-qbv-on.gif)

Qbv takes about 10 s to turn on and up to 30 s to turn off; the toggle shows the real switch state.

```bash
make status
make down
```

## Basler testing

Use two Basler GigE cameras instead of the test videos for use case 2. Prepare them first:
[Configure Basler PTP timestamps](./how-to-guides/basler/configure-basler-ptp-timestamps.md) and
[Integrate Basler cameras with SceneScape](./how-to-guides/basler/integrate-basler-camera-with-scenescape.md),
which also explains how to build the pipeline server image with the Basler SDK yourself (the project does not ship it).

```bash
make up SOURCE=basler
make down
```

The test-video streams still start: use case 1 needs them on machines 2 and 3 (do not pass
`STREAMS=0`). Always pass `SOURCE=basler` on the command line; the default is `video`.
HOTA is not shown for Basler (no ground truth); the tracker rate is.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Browser shows `400 Bad Request` | The host in the URL is not allowed; use the lab address printed by `make up` |
| Qbv toggle disabled with a banner | Turn on **Schedule Gate** for port 1 in the switch web page |
| Qbv toggle disabled, read-only | Set `SWITCH_ALLOW_WRITE=1` in `src/dashboard/.env`, run `make up` again |
| Latency stays high with Qbv on | Check the VLAN priority maps (`ip -d link show tsn0.1` lists `egress_qos`) and PTP (`sudo ptp-profile status`) |
| Basler `Device::open()` fails | Another process holds the camera: `docker stop -t 30` the pipeline container, then retry |
| A start failed half way | `make down`, fix the cause, `make up` again. `make -C src/scene clean` also deletes the SceneScape database volumes |

## Key References

- [MOXA TSN-G5000 series](https://www.moxa.com/en/products/industrial-network-infrastructure/ethernet-switches/layer-2-managed-switches/tsn-g5008-series)
- [SceneScape](https://github.com/open-edge-platform/scenescape)
- [DL Streamer](https://github.com/open-edge-platform/dlstreamer)
- [TrackEval (HOTA)](https://github.com/JonathonLuiten/TrackEval)
- [First-time setup](./first-time-setup.md)
- [How-to guides](./how-to-guides.md)

<!--hide_directive
:::{toctree}
:hidden:

system-requirements
first-time-setup
how-to-guides

:::
hide_directive-->

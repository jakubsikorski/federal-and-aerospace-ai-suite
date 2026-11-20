<!--hide_directive
```{eval-rst}
.. meta::
   :description: Machine, network, software, and hardware requirements for the four machines and the Moxa Time-Sensitive Networking (TSN) switch.
   :keywords: system requirements, Moxa TSN-G5000, Time-Sensitive Networking, TSN, VLAN, Basler, Docker, Ubuntu 24.04
```
hide_directive-->

# System Requirements

## Machines

Four Linux machines, all cabled to the same Moxa Time-Sensitive Networking (TSN) switch and
to a lab network:

| Machine   | Role                                                                                                            | Switch port |
| --------- | --------------------------------------------------------------------------------------------------------------- | ----------- |
| machine-1 | Dashboard, Scenescape, Deep Learning Streamer (DL Streamer), MQTT broker. Also the machine where you run `make` | 1           |
| machine-2 | Camera 1: test-video stream and inference                                                                       | 2           |
| machine-3 | Camera 2: test-video stream and inference                                                                       | 3           |
| machine-4 | Load injector                                                                                                   | 4           |

Each machine needs an Intel I225-LM or I226-LM TSN network card (PCI ID `8086:15f2` / `8086:125b`).
Machine-1 needs about 8 CPU cores for the full stack and several GB of free disk space for the Docker
images (the DL Streamer image alone is about 5 GB). The camera machines need Docker and enough CPU
for one inference pipeline each.

## Network Topology

![Network topology: four machines, the Moxa TSN switch, optional Basler cameras, and the lab network](_assets/network-topology.svg)

Ports 1 to 4 carry VLAN 1 (camera data, priority 1), VLAN 3 (Basler cameras), and VLAN 5 (load, priority 5).
The dotted lab network is never shaped.

## Software

- Ubuntu 24.04
- Docker Engine with Docker Compose V2 on all machines, so that `docker compose` works
  (not only the legacy `docker-compose`). For installation instructions, see the
  [installation guide](https://docs.docker.com/engine/install/ubuntu/).
- `git` and `make` on machine-1
- `linuxptp` and NetworkManager (installed or used by the setup scripts)
- Passwordless, key-based SSH from machine-1 to machines 2, 3, and 4
- Internet or proxy access on machine-1 to pull the Docker images and to download the detection model.
  The download script (`src/data/download_models.sh`) runs automatically in `make up`. The model comes
  from Open Model Zoo (Apache-2.0), and `make up` asks you to accept its license on the first
  download.

## Network

- **Lab network:** SSH, the dashboard in the browser, and control traffic. Not part of the TSN path.
- **TSN switch:** VLAN 1 (`192.168.127.x`) camera data, VLAN 5 (`192.168.5.x`) background load,
  VLAN 3 (`192.168.3.x`) Basler cameras.

## Default Values and Assumptions

`lab.env` holds the lab addresses (see [First-time setup](./first-time-setup.md)), but some defaults
in the scripts and configuration files match the reference setup. To reproduce the demo without
editing them, use the same values:

| Item                          | Value                                                                  | Where it is set                                                  |
| ----------------------------- | ---------------------------------------------------------------------- | ---------------------------------------------------------------- |
| Switch ports                  | machine-1 on 1, machine-2 on 2, machine-3 on 3, machine-4 on 4        | `SWITCH_PORT` (default 1, machine-1); port 2 is never changed    |
| Camera data VLAN 1 addresses  | machine-1 `192.168.127.11`, machine-2 `.12`, machine-3 `.13`, switch `.253` | `lab.env`; also `mediamtx.yml` and `CAMERA1_HOST`/`CAMERA2_HOST` in `src/scene/.env` |
| Load VLAN 5 addresses         | machine-1 `192.168.5.11`, machine-4 `192.168.5.14`                    | `lab.env`                                                        |
| Basler VLAN 3                 | cameras `192.168.3.17` and `.18` on switch ports 5 and 6, `tsn0.3` on machine-1 | `src/scene/.env`, `BASLER_PORTS`                          |
| NIC interface                 | `tsn0` on every machine, with `tsn0.1`, `tsn0.3`, and `tsn0.5` VLAN interfaces | `src/setup/setup_tsn_nic.sh`, `TSN_IFACE` in `src/scene/Makefile` |
| SSH user and key              | The same user on machines 2, 3, and 4, with key-based login           | `SSH_USER` in `lab.env`; `EDGE_HOSTS` in `src/camera-node/pipeline/deploy.sh` and `SWITCH_HOST` in `src/dashboard/Makefile` default to the reference lab's user and addresses |
| Camera 1 and camera 2         | machine-2 publishes `Cam_x1_0`, machine-3 publishes `Cam_x2_0`        | `EDGE_HOSTS`, `STREAM_HOSTS`                                     |
| Qbv                           | Schedule Gate on for the machine-1 port, schedule `90000:6,1,0 10000:5,1,0` | `SWITCH_SCHEDULE` in the dashboard                         |
| Basler cameras                | serials `23117381` and `23117382`, 15 frames per second               | `src/scene/.env`                                                 |

If your lab differs, set `EDGE_HOSTS` and `SWITCH_HOST` on the command line, and change the two
camera addresses in `src/scene/mediamtx/mediamtx.yml` and `src/scene/.env`.

## Hardware

| Component                          | Details                                                                                                                                              |
| ---------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| Moxa TSN-G5000 switch              | IEEE 802.1Qbv and IEEE 1588v2/802.1AS support, Telnet management enabled (the dashboard scripts use it). Tested with TSN-G5008-2GTXSFP, firmware 2.1 |
| Basler ace GigE cameras (optional) | Precision Time Protocol (PTP) hardware timestamps, for the Basler test only. Tested with acA1920-40GC                                                |

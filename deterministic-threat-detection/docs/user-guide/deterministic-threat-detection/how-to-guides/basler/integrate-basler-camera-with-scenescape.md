<!--hide_directive
```{eval-rst}
.. meta::
   :description: Use two Basler GigE cameras as the video sources for use case 2 by building the Docker image with the Basler camera SDK and running the pipeline server.
   :keywords: Basler, GigE, Scenescape, pylon SDK, gencamsrc, pipeline server, macvlan, VLAN 3
```
hide_directive-->

# Integrate Basler Cameras with Scenescape

This guide shows you how to use two Basler GigE cameras as the video sources for
use case 2. You add a Dockerfile for the image with the Basler camera SDK, and then
run it to start the pipeline server on a network that can reach the cameras.

> [!IMPORTANT]
> The pylon SDK is proprietary software under Basler's own license terms (see
> <https://www.baslerweb.com/en/downloads/software/>). It is not part of this project and not
> covered by its Apache-2.0 license. By building the image, you accept Basler's terms. Build it
> locally for your own cameras and do not publish or redistribute it. Do not commit `src/scene/basler/`. `gst-plugin-pylon`
> (BSD-3-Clause) and `gencamsrc` (Apache-2.0) are open source.

## Prepare (Once)

1. Configure the cameras: [Configure Basler PTP timestamps](./configure-basler-ptp-timestamps.md).
2. Create VLAN 3 on machine-1, as described in [First-time setup](../../first-time-setup.md).
   The cameras are in VLAN 3 on switch ports 5 and 6.
3. Assign priority 0 to the camera ports. Untagged camera frames take the port's priority.
   Priority 3 would put the camera video in the same queue as other VLAN 3 traffic and starve
   MQTT under the IEEE 802.1Qbv Time-Aware Shaper (Qbv) schedule.

   `make up SOURCE=basler` does this for you: it sets ports 5 and 6 to priority 0 when they differ
   (it changes only these ports, over machine-2; `BASLER_PORTS=<list>` selects other ports and
   `BASLER_PRIORITY_FIX=0` skips the step). To do it by hand, run the following on machine-2 after
   `make -C src/dashboard deploy-switch` (`<switch address>` is `SWITCH_IP` from `lab.env`):

   ```bash
   python3 -I ~/moxa/port_priority.py set --host <switch address> --ports 5,6 --value 0
   python3 -I ~/moxa/port_priority.py get --host <switch address> --ports 5,6
   ```

4. Add the Dockerfile for the pipeline server image with the camera SDK. The standard Deep Learning
   Streamer Pipeline Server (DL Streamer Pipeline Server) image has neither the pylon SDK nor the
   `gencamsrc` GStreamer source, and the project does not ship an image that has them.

   Save the following as `src/scene/basler/BaslerDockerfile` (create the directory first):

   ```dockerfile
   # SPDX-FileCopyrightText: (C) 2026 Intel Corporation
   # SPDX-License-Identifier: Apache-2.0
   #
   # DL Streamer Pipeline Server with the Basler pylon runtime and the gencamsrc GStreamer source, built with
   # the camera hardware (PTP) timestamp attached to every frame (GstReferenceTimestampMeta, timestamp/x-hardware).
   #
   # The pylon SDK is proprietary software under Basler's own license (https://www.baslerweb.com/en/downloads/software/),
   # not covered by this project's Apache-2.0 license. By building this image you accept Basler's terms; build it
   # locally for your own cameras and do not publish or redistribute it.
   ARG BASE_IMAGE
   FROM ${BASE_IMAGE}

   ARG PYLON_VERSION=7.5.0.15658
   ARG PYLON_URL=https://downloads-ctf.baslerweb.com/dg51pdwahxgw/16EbjATpV78LtOFUQ1VpJM/ab3db40227afb59df3eb1cccf0c5addc/pylon-${PYLON_VERSION}-linux-x86_64_debs.tar.gz
   ARG GST_PLUGIN_PYLON_URL=https://github.com/basler/gst-plugin-pylon/releases/download/v1.0.0/gst-plugin-pylon_1.0.0-1.ubuntu-24.04_amd64.deb
   # gencamsrc source, mainline of edge-ai-libraries (pinned commit)
   ARG EAL_REPO=https://github.com/open-edge-platform/edge-ai-libraries
   ARG EAL_REF=cc2ac98fb0d3de12e5b12db37ce8fea34aa4d75a

   USER root
   RUN apt-get update && apt-get install -y --no-install-recommends wget ca-certificates git gnupg cmake make g++ \
         gstreamer1.0-plugins-base libgstreamer-plugins-base1.0-dev libgstreamer1.0-dev libxcb-cursor0 \
       && apt-get clean && rm -rf /var/lib/apt/lists/*

   RUN cd /tmp && wget -q "${PYLON_URL}" -O pylon.tar.gz && tar -xzf pylon.tar.gz \
       && (dpkg -i pylon_${PYLON_VERSION}-deb0_amd64.deb || apt-get install -fy) \
       && rm -rf /tmp/pylon* /var/lib/apt/lists/*

   RUN cd /tmp && wget -q "${GST_PLUGIN_PYLON_URL}" -O gst-plugin-pylon.deb \
       && dpkg -i gst-plugin-pylon.deb && rm gst-plugin-pylon.deb

   RUN cd /tmp && git init -q eal && cd eal && git remote add origin "${EAL_REPO}" \
       && git sparse-checkout set microservices/dlstreamer-pipeline-server/plugins/camera/src-gst-gencamsrc \
       && git fetch -q --depth 1 --filter=blob:none origin "${EAL_REF}" && git checkout -q FETCH_HEAD \
       && cp -r microservices/dlstreamer-pipeline-server/plugins/camera/src-gst-gencamsrc /home/pipeline-server/src-gst-gencamsrc \
       && cd /home/pipeline-server/src-gst-gencamsrc \
       && cmake -B build -DCMAKE_BUILD_TYPE=Release -DGENCAMSRC_ENABLE_HW_TIMESTAMP_META=ON \
       && cmake --build build -j"$(nproc)" && cmake --install build && ldconfig \
       && rm -rf /tmp/eal /home/pipeline-server/src-gst-gencamsrc/build

   ENV GENICAM_GENTL64_PATH=/opt/pylon/lib/gentlproducer/gtl \
       GST_PLUGIN_PATH=/opt/intel/dlstreamer/lib:/opt/intel/dlstreamer/gstreamer/lib/gstreamer-1.0:/opt/intel/dlstreamer/gstreamer/lib/:/usr/lib/x86_64-linux-gnu/gstreamer-1.0:/usr/local/lib/gstreamer-1.0
   USER intelmicroserviceuser
   ```

   The first `make up SOURCE=basler` builds the image from it as
   `tsn-dlstreamer-pipeline-server-basler:<SCENESCAPE_VERSION>`, using
   `DLSTREAMER_PIPELINE_SERVER_IMAGE` and `SCENESCAPE_VERSION` from `src/scene/.env` (later runs reuse
   it; delete the image to rebuild). The Dockerfile:

   - Builds `gencamsrc` with `-DGENCAMSRC_ENABLE_HW_TIMESTAMP_META=ON`, so it attaches the camera's
     Precision Time Protocol (PTP) hardware timestamp to every frame (`GstReferenceTimestampMeta`).
     The Scenescape adapter uses it as the frame time. It is pinned to the `edge-ai-libraries`
     mainline commit `cc2ac98fb0d3de12e5b12db37ce8fea34aa4d75a`.
   - Downloads and installs the pylon runtime (version 7.5) and `gst-plugin-pylon`.
   - Installs the build tools for `gencamsrc` (`cmake`, `make`, `g++`, and the GStreamer development
     packages). The `ubuntu24` base image no longer contains `make`.
   - Sets `GENICAM_GENTL64_PATH` and `GST_PLUGIN_PATH`, and switches back to the base image's
     non-root user (`intelmicroserviceuser`).

5. In `src/scene/.env`, set the camera serial numbers and, if needed, the macvlan address, which
   must be a free address in the VLAN 3 subnet. The first `make up` creates the file from
   `.env.example`, so run `make up` once first if the file does not exist yet:

   ```text
   BASLER_CAMERA1_SERIAL=<serial of camera 1>
   BASLER_CAMERA2_SERIAL=<serial of camera 2>
   BASLER_CONTAINER_IP=192.168.3.61
   BASLER_FPS=15
   ```

## Run

```bash
make up SOURCE=basler
make down
```

Always pass `SOURCE=basler`, because a plain `docker compose up` has no camera plugin and the
pipelines fail with `no element "gencamsrc"`.

![Dashboard with Basler cameras](../../_assets/uc2-tracking-basler.gif)

The Architecture view shows the two cameras on switch ports 5 and 6. With Qbv off, the load
starves them. With Qbv on, both keep their frame rate.

![Basler, no load](../../_assets/architecture-basler-idle.gif)

![Basler, load, Qbv off](../../_assets/architecture-basler-load-qbv-off.gif)

![Basler, load, Qbv on](../../_assets/architecture-basler-load-qbv-on.gif)

The dashboard shows the tracker rate and the decoded frames. Higher Order Tracking Accuracy
(HOTA) is not meaningful here because there is no ground truth and no frame number.

## What make up SOURCE=basler Does

- Starts the pipeline server from your `tsn-dlstreamer-pipeline-server-basler` image.
- Attaches the container to a macvlan network on `tsn0.3` so it can reach the cameras.
- Runs `gencamsrc ... ! gvapython` with `src/scene/dlstreamer-pipeline-server/sscape_adapter.py`,
  which publishes the camera's PTP hardware timestamp as the frame time.

## Network load limit

The Basler cameras stream over GigE Vision, which does not tolerate packet loss. With Qbv off, a
high network load can make camera detection fail (`No frame received from the camera`), and the
camera does not recover by itself.

100 Mbit/s is recommended when testing with Basler cameras. You can try a higher load, but camera
detection might fail. The limit depends on the hardware, so raise the load in small steps and watch
the camera frame rate in the dashboard. If a camera does not recover, set the load to 0 and restart
the pipeline server (DLSPS) as described in [Troubleshooting](#troubleshooting).

## Troubleshooting

- A camera stops delivering frames and does not recover (the dashboard shows 0 fps and the pipeline
  restart counter keeps rising, or the log shows `Camera ... not detected` or
  `Requested resource is already in use`): set the network load to 0, then restart the pipeline server.
  Stop it with `docker stop -t 30 tsn-scene-dlstreamer-pipeline-server-1`, wait about 20 seconds so the
  cameras release, then run `docker start tsn-scene-dlstreamer-pipeline-server-1`. If it exits with
  `Device::open() failed`, wait a little longer and start it again.

- `Device::open()` fails: the camera is still held by another process. Stop the pipeline container with
  `docker stop -t 30`, wait, then run `make up SOURCE=basler` again.
- The build fails downloading `gst-plugin-pylon` (`wget` exit code 5, certificate error): a proxy that
  re-signs HTTPS traffic breaks the GitHub release redirect to `release-assets.githubusercontent.com`.
  Download the `.deb` on the host into `src/scene/basler/gst-plugin-pylon.deb`, and in the Dockerfile
  replace the `wget` line of that step with `COPY gst-plugin-pylon.deb /tmp/gst-plugin-pylon.deb`.
- Timestamps from 1970: the camera's PTP is not locked yet (timestamp 0 is ignored by the adapter).
- No cameras found: run `ping 192.168.3.17` from machine-1 and check the camera IP addresses.

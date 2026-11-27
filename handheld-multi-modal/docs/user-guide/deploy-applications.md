<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Handheld Multi-Modal Application Deployment

This guide provides instructions on how to deploy the Handheld Multi-Modal application with custom settings on a
local machine.

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Intel® Core™ Ultra processor with integrated GPU and NPU | Drivers must expose `/dev/dri/renderD128` (GPU) and `/dev/accel/accel0` (NPU). See [Install OEP SDKs](install-oep-sdks.md) to verify the accelerators. |
| Docker Engine and Docker Compose 2.24.4 or later | [Install guide](https://docs.docker.com/engine/install/). Use `docker compose` (space), not `docker-compose` (hyphen). The user must be able to run `docker` without `sudo`. |
| GNU Make, Bash, Python 3.11 or later, curl, OpenSSL | Standard Linux utilities used by the Makefile. |
| Disk space and Internet access for the first start | The first start downloads several GB of container images and models. Later starts run offline. |

The Makefile detects the GPU, NPU, and asset-directory group IDs separately; they do not need
to match.

## Installing application

If `openedge-cli` wasn't installed in previous steps, start by installing it:

```bash
curl -fsSL https://raw.githubusercontent.com/open-edge-platform/edge-ai-libraries/refs/heads/release-2026.3.0/frameworks/oep-installer-cli/rendered/openedge-cli > /tmp/openedge-cli
sudo chmod +x /tmp/openedge-cli
sudo mv /tmp/openedge-cli /usr/bin
```

Optionally, export a Hugging Face token. The configured models are public and do not require it; a token only helps when Hugging Face rate-limits anonymous downloads. Do not store the token in configuration files or commit it to version control.
```bash
export HF_TOKEN=hf_...
```

After that, install application:

```bash
openedge-cli install handheld_multi_modal
```

After installation is done, start application by running

```bash
openedge-cli start handheld_multi_modal
```

## Deploying the Application with different settings

If you want to use application with non-default settings, that are described in [openedge-cli start](./infrastructure/installation-on-ubuntu.md#step-3-start-the-application), you can modify options as following:

Go into application folder

```bash
cd ~/.local/open-edge-platform/handheld-multi-modal
```

Start the application with the looping demo camera feeds:

```bash
make demo-up
```

Or start it without demo cameras, and add your own cameras later in the console:

```bash
make up
```

Both targets check the host and then list the third-party container images, Python packages,
models, and (for `make demo-up`) demo videos they download, with their licenses, and ask you
to accept them. Answer `y` to continue, or pass `CONFIRM=yes` to accept without the question,
for example in scripts: `make demo-up CONFIRM=yes`. The list is generated from
[`third-party-components.spdx`](../../third-party-components.spdx).

They then download the YOLO26s INT8 detector through the Model Download
microservice, generate a self-signed TLS certificate for `localhost`, build the Backend image,
and start every service. `make demo-up` also downloads the demo videos and waits until both
demo cameras produce detections and annotated video. While waiting, Make prints progress and
the latest error every 30 seconds.

The first start downloads and converts the language, speech-to-text, and text-to-speech models
through their stock microservices and can take up to 90 minutes, depending on network bandwidth.
Later starts use the cached models and do not need Internet access.

Run `make` on its own to list all commands. Use `CAMERAS=1` to start a single demo camera and
`TTS_DEVICE=GPU` to run text-to-speech on the GPU instead of the CPU, for example:

```bash
make demo-up CAMERAS=1
```

Optionally, load the synthetic example mission document:

```bash
make demo-mission
```

## Verifying the Installation

Show the services and per-camera feed health:

```bash
make status
```

Run the read-only readiness check. It verifies images, models, and demo videos and, once the
application is started, that every service answers, each enabled camera produces detections
and annotated video, and the language and speech models respond. Each failing item names the
command that fixes it:

```bash
make check
```

Then open the operator console as described in
[Access Application User Interface](access-application.md).

## Stopping and Removing the Application

```bash
make stop                      # stop every service; nothing is deleted
make down                      # remove containers; keep models, history, and TLS keys
make clean-all CONFIRM=yes     # also delete history, downloaded models, caches, and TLS keys
```

See [Application Operations](application-operations.md) for all lifecycle commands, offline
installation on other devices, and model changes.

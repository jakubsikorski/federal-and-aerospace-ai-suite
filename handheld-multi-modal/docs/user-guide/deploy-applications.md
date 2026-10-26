# Handheld Multi-Modal Application Deployment

This guide provides instructions on how to deploy the Handheld Multi-Modal application on a
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

## (Optional) Configuring the Proxy

> [!NOTE]
> If not using proxy servers, skip to [Deploying the Application](#deploying-the-application).

Depending on the system's network configuration, you may need an additional proxy configuration.
Ensure that `/etc/environment` contains proxy variables; replace `proxy-example:123` with a
valid proxy for the local environment. `localhost` and `127.0.0.1` must be in `no_proxy`:
the pre-flight check fails if requests to `127.0.0.1` would go through the proxy.

```bash
sudo tee -a /etc/environment > /dev/null <<EOT
http_proxy="http://proxy-example:123"
https_proxy="http://proxy-example:123"
no_proxy="localhost,127.0.0.1"
HTTP_PROXY="http://proxy-example:123"
HTTPS_PROXY="http://proxy-example:123"
NO_PROXY="localhost,127.0.0.1"
EOT

source /etc/environment
```

Configure the proxy for the Docker daemon, so that it can pull images:

```bash
sudo mkdir -p /etc/systemd/system/docker.service.d
sudo tee /etc/systemd/system/docker.service.d/http-proxy.conf > /dev/null <<EOT
[Service]
Environment="HTTP_PROXY=http://proxy-example:123"
Environment="HTTPS_PROXY=http://proxy-example:123"
Environment="NO_PROXY=localhost,127.0.0.1"
EOT

sudo systemctl daemon-reload
sudo systemctl restart docker
```

Verify the Docker daemon's proxy configuration:

```bash
docker info | grep -i proxy
```

## Deploying the Application

Download the compressed file:

```bash
curl -OjL https://github.com/open-edge-platform/federal-and-aerospace-ai-suite/releases/download/fedaero-latest/handheld-multi-modal.zip
```

Decompress the downloaded file and enter the application directory:

```bash
unzip handheld-multi-modal.zip
cd handheld-multi-modal
```

Optionally, export a Hugging Face token. The configured models are public and do not require
it; a token only helps when Hugging Face rate-limits anonymous downloads. Do not store the
token in configuration files or commit it to version control.

```bash
export HF_TOKEN=hf_...
```

Start the application with the looping demo camera feeds:

```bash
make demo-up
```

Or start it without demo cameras, and add your own cameras later in the console:

```bash
make up
```

Both targets check the host, download the YOLO26s INT8 detector through the Model Download
microservice, generate a self-signed TLS certificate for `localhost`, build the Backend image,
and start every service. `make demo-up` also downloads the demo videos and waits until both
demo cameras produce detections and annotated video. While waiting, Make prints progress and
the latest error every 30 seconds.

The first start downloads and converts the language, speech-to-text, and text-to-speech models
through their stock microservices and can take up to an hour, depending on network bandwidth.
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

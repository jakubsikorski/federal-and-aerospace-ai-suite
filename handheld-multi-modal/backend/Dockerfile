# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

# Test image for every offline `make test-*` target, so the host needs only
# Docker and Make: the runtime dependencies plus coverage, Make (asset dry-run
# checks), and headless Firefox ESR with a pinned geckodriver (browser checks).
# Sources are mounted read-only when it runs. The runtime stage below stays
# last, so Compose builds it unchanged and never this one.
FROM python:3.12-slim AS test
ARG GECKODRIVER_VERSION=0.37.1
ARG GECKODRIVER_SHA256=e815130ea95983e162ae91843b48d3a3ce991735635fce83a647afde21e09f7e
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends make firefox-esr fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*
RUN python3 - <<EOF
import hashlib, io, tarfile, urllib.request
url = "https://github.com/mozilla/geckodriver/releases/download/v${GECKODRIVER_VERSION}/geckodriver-v${GECKODRIVER_VERSION}-linux64.tar.gz"
data = urllib.request.urlopen(url, timeout=120).read()
assert hashlib.sha256(data).hexdigest() == "${GECKODRIVER_SHA256}", "geckodriver checksum mismatch"
tarfile.open(fileobj=io.BytesIO(data)).extract("geckodriver", "/usr/local/bin", filter="data")
EOF
ENV GECKODRIVER=/usr/local/bin/geckodriver HOME=/tmp
WORKDIR /srv/backend
COPY requirements.txt requirements-test.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-test.txt \
    && pip uninstall -y pip
USER 65534:65534
CMD ["python3", "test_backend.py"]

FROM python:3.12-slim
# Pick up Debian security fixes released after the base image was published.
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
# pip is build-only; removing it drops its vendored urllib3/msgpack/setuptools.
RUN pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y pip
COPY app ./app
# Self-check ships with the image so it runs against the same deps as the service.
COPY test_backend.py .
EXPOSE 8100
# Runs as root on purpose: per-camera reset must run the crop producer's CLI as
# the crop store's owner (DLSPS UID 1999, directories may be mode 2700), and
# /data belongs to the host user. Compose drops every capability except the
# ones that needs (see the backend service in compose/handheld-multi-modal.yaml).
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python3", "-c", "import urllib.request as u; u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:8100/api/health', timeout=8)"]
CMD ["python3", "-m", "app"]

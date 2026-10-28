# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

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

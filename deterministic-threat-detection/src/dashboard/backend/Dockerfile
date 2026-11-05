# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

FROM python:3.11-slim

# Same UID/GID as the host user so the mounted ~/.ssh keys keep working (set by the Makefile).
ARG UID=1000
ARG GID=1000

# iperf3 server for the VLAN 5 load, ssh client for machine-2 (switch) and machine-4 (injector), curl to fetch Chart.js.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends iperf3 openssh-client curl \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -g ${GID} app && useradd -m -u ${UID} -g ${GID} -s /bin/sh app

# Fresh container, fresh known_hosts: learn host keys on first use, never write to the mounted ~/.ssh.
RUN printf 'Host *\n  UserKnownHostsFile /tmp/known_hosts\n  GlobalKnownHostsFile /dev/null\n  StrictHostKeyChecking accept-new\n' \
    > /etc/ssh/ssh_config.d/dashboard.conf

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY static static

# Chart.js (MIT) is downloaded at build time, not stored in the repo.
RUN mkdir -p static/js/vendor \
 && curl -fsSL -o static/js/vendor/chart.umd.js https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.js \
 && chown -R app:app static

USER app
CMD ["sh", "-c", "exec uvicorn app.main:app --host ${DASHBOARD_BIND:-0.0.0.0} --port ${DASHBOARD_PORT:-8080}"]

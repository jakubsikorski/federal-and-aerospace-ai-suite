# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
FROM python:3.11-slim
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends iperf3 \
 && rm -rf /var/lib/apt/lists/*
COPY load_agent.py /srv/load_agent.py
USER nobody
CMD ["python3", "-u", "/srv/load_agent.py"]

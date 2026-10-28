<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Agent SKILLs

This section covers the AI agent skills and coding-agent workflows available for the Handheld
(Soldier System) Blueprint — from platform-level automation (image build, provisioning, power
and thermal tuning) to application-level pipeline generation with Intel DL Streamer.

- [DL Streamer Pipelines Guide](infrastructure/build-dlstreamer-pipelines.md) — Building,
  running, and troubleshooting DL Streamer video analytics pipelines, including AI-assisted
  pipeline generation with the DL Streamer Coding Agent.
- [Infrastructure AI Agent Integration](infrastructure/agent-skills.md) — Available
  agent skills for building images, provisioning, and power/thermal tuning, and how to run them
  from a developer host or directly on a provisioned target.
- Application skills — the `handheld-multi-modal/.agents/skills` directory bundles skills for
  the application architecture, camera connection, the DL Streamer Pipeline Server contract,
  and the Open Edge Platform microservices it uses.
- Upstream skills — for the stock services, use the Open Edge Platform skills directly:
  [`chatqna-docker-deploy`](https://github.com/open-edge-platform/skills/tree/main/.agents/skills/chatqna-docker-deploy),
  [`dlsps-user`](https://github.com/open-edge-platform/skills/tree/main/.agents/skills/dlsps-user),
  [`dlstreamer-coding-agent`](https://github.com/open-edge-platform/skills/tree/main/.agents/skills/dlstreamer-coding-agent) and
  [`model-download-user`](https://github.com/open-edge-platform/skills/tree/main/.agents/skills/model-download-user). The application skills take
  precedence where they differ.

<!--hide_directive
:::{toctree}
:hidden:

DL Streamer Pipelines Guide <infrastructure/build-dlstreamer-pipelines.md>
Infrastructure AI Agent Integration <infrastructure/agent-skills.md>
:::
hide_directive-->

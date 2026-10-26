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
  and the Open Edge Platform microservices it uses. The `chatqna-docker-deploy`, `dlsps-user`,
  `dlstreamer-coding-agent` and `model-download-user` skills are copies from
  [open-edge-platform/skills](https://github.com/open-edge-platform/skills).

<!--hide_directive
:::{toctree}
:hidden:

DL Streamer Pipelines Guide <infrastructure/build-dlstreamer-pipelines.md>
Infrastructure AI Agent Integration <infrastructure/agent-skills.md>
:::
hide_directive-->

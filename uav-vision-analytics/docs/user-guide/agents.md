# Agent SKILLs

This section covers the AI agent skills and coding-agent workflows available for the Uncrewed
Aerial Vehicle (UAV) Blueprint — from platform-level automation (image build, provisioning,
power and thermal tuning) to application-level pipeline generation with Intel DL Streamer.

- [DL Streamer Pipelines Guide](infrastructure/build-dlstreamer-pipelines.md) — Building,
  running, and troubleshooting DL Streamer video analytics pipelines, including AI-assisted
  pipeline generation with the DL Streamer Coding Agent.
- [Infrastructure AI Agent Integration](infrastructure/agent-skills.md) — Available
  agent skills for building images, provisioning, and power/thermal tuning, and how to run them
  from a developer host or directly on a provisioned target.
- [SDK Agent Commands and MCP Tools](infrastructure/uav-sdk-apps-skill.md) — Claude Code
  slash commands that drive the UAV Mission Compute SDK stack (start, validate, capture,
  switch camera, cleanup), plus the MCP server tools exposing Anomalib, DL Streamer,
  Edge AI Suites, and live MAVLink telemetry.
- Application skills — both Vision Analytics and Ground Station apps come with skills for
  the `uav-ground-station/.github/skills/uav-ground-station` directory
  bundles skills for setting up the applications and the Open Edge Platform microservices
  they use. You can find the skill files under:
  [uav-vision-analytics/.github/skills](https://github.com/open-edge-platform/federal-and-aerospace-ai-suite/tree/main/uav-vision-analytics/.github/skills)
  and
  [uav-ground-station/.github/skills](https://github.com/open-edge-platform/federal-and-aerospace-ai-suite/blob/main/uav-ground-station/.github/skills/uav-ground-station/SKILL.md)
- Upstream skills — for the stock services, use the Open Edge Platform skills directly:
  [`chatqna-docker-deploy`](https://github.com/open-edge-platform/skills/tree/main/.agents/skills/chatqna-docker-deploy),
  [`dlsps-user`](https://github.com/open-edge-platform/skills/tree/main/.agents/skills/dlsps-user),
  [`dlstreamer-coding-agent`](https://github.com/open-edge-platform/skills/tree/main/.agents/skills/dlstreamer-coding-agent) and
  [`model-download-user`](https://github.com/open-edge-platform/skills/tree/main/.agents/skills/model-download-user).
  The application skills take precedence where they differ.


<!--hide_directive
:::{toctree}
:hidden:

DL Streamer Pipelines Guide <infrastructure/build-dlstreamer-pipelines.md>
Infrastructure AI Agent Integration <infrastructure/agent-skills.md>
SDK Agent Commands and MCP Tools <infrastructure/uav-sdk-apps-skill.md>
:::
hide_directive-->

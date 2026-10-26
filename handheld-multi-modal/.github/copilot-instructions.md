<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Repository Instructions

Follow [AGENTS.md](../AGENTS.md). This is the Handheld Multi-Modal application, built on
unmodified Open Edge Platform `release-2026.2.0` microservices.

Use the stock Chat Q&A `/chat` endpoint for general and document questions with the configured
prompt template. Do not clear the mission document corpus for general chat and do not claim
conversation memory that the service does not provide.

Use the skills in `.agents/skills/`. When using the upstream
[open-edge-platform/skills](https://github.com/open-edge-platform/skills), do not apply their
default models or floating-tag examples; the model choices in `AGENTS.md` override them.

Smoke commands and their scope are listed in `scripts/smoke/README.md`. Never replace missing
service contracts with plausible APIs or mark blocked scripts as working integrations.

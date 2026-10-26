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

Use the pinned skills in `.agents/skills/`, but do not apply their default models or
floating-tag examples. The model choices and provenance rules in `AGENTS.md` and
`MODEL_PROVENANCE.md` override generic upstream examples.

Smoke commands and their scope are listed in `scripts/smoke/README.md`. Never replace missing
service contracts with plausible APIs or mark blocked scripts as working integrations.

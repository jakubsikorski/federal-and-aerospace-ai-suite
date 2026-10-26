<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Copilot Instructions for Federal and Aerospace AI Suite

## Repository Overview

This repository contains AI-enabled applications and supporting components for aerospace and defense edge deployments. Component directories are directly under the repository root:

- [handheld-multi-modal/](../handheld-multi-modal/README.md) — Localhost operator console fusing live video analytics, mission documents, alerts, and speech
- [deterministic-threat-detection/](../deterministic-threat-detection/README.md) — Deterministic, low-latency AI video and sensor processing using Time-Sensitive Networking (preview)
- [uav-mission-compute-sdk/](../uav-mission-compute-sdk/README.md) — UAV simulation, telemetry, camera streaming, and edge AI infrastructure
- [uav-vision-analytics/](../uav-vision-analytics/README.md) — UAV object detection with correlated MAVLink telemetry overlays

### Navigation

When working on a specific component, check for a local `AGENTS.md` file first — it contains authoritative per-component instructions:
- [handheld-multi-modal/AGENTS.md](../handheld-multi-modal/AGENTS.md)

Read the [root README](../README.md) for suite-level context and the active component's `README.md` for setup and usage details. User guides are under each component's `docs/user-guide/` directory. Paths in workflows and root build commands are relative to this repository, without an additional `federal-and-aerospace-ai-suite/` prefix.

## Languages & Frameworks

- **Primary language:** Python (Flask and FastAPI services, inference, telemetry, and pipeline management)
- **Scripts:** Bash for deployment, setup, and validation
- **UI and observability:** Open WebUI, Grafana, NGINX, and application HTML/JavaScript
- **Infrastructure:** Docker and Docker Compose
- **ML/AI:** OpenVINO, OVMS (OpenVINO Model Server), Open Edge Platform microservices (Chat Q&A, Audio Analyzer, Text To Speech), HuggingFace, Intel DL Streamer
- **UAV and streaming:** PX4, Gazebo, MAVLink, MQTT, and RTSP

## Build Systems

- **Make** orchestrates packaging and component lifecycle tasks. The [root Makefile](../Makefile) builds component and full-suite ZIP archives into `dist/`; it is not a deployment entry point. Run `make help` in the active component where available.
- **Docker Compose** (`docker compose`) runs application services. Follow the component's documented Compose files, prerequisites, and `.env` configuration; filenames and deployment targets differ between components.
- **Handheld deployment:** Follow [AGENTS.md](../handheld-multi-modal/AGENTS.md) for `make up` or `make demo-up`, and `make check` for readiness. Every host port binds to `127.0.0.1`.
- **UAV deployment:** Follow the [SDK user guide](../uav-mission-compute-sdk/docs/user-guide/get-started.md) and [analytics user guide](../uav-vision-analytics/docs/user-guide/index.md) for the selected camera and telemetry mode.
- **Python packaging:** Mix of `requirements.txt`, `pyproject.toml` (with setuptools or uv), and `pip`.

## Testing Patterns

- Follow the active component's documented checks and existing test conventions; do not assume a shared root-level test suite or uniform `tests/unit/` layout.
- [uav-mission-compute-sdk/pyproject.toml](../uav-mission-compute-sdk/pyproject.toml) contains pytest configuration. Verify that configured test paths and dependencies are available before running tests.
- [handheld-multi-modal/backend/](../handheld-multi-modal/backend/) contains Backend tests and REST API fuzzing; its Makefile provides `test`, `test-ui`, `test-assets`, `test-fuzz`, and `test-history` targets, plus smoke tests in `scripts/smoke/`.
- For pytest tests, use `test_*.py` files, `Test*` classes, and `test_*` functions. Reuse existing fixtures and markers where present, and use environment-based test configuration rather than hardcoding credentials.
- Run the smallest relevant check after editing. Distinguish static checks from hardware-dependent or running-service validation, and report checks that could not be run.

## Linting & Formatting

- **Ruff** (lint + format) — configured in [uav-mission-compute-sdk/pyproject.toml](../uav-mission-compute-sdk/pyproject.toml); follow the active component's configuration
- **ShellCheck** — for all `.sh` files
- **Bandit** — Python security static analysis
- Preserve existing formatting and validate structured files with appropriate YAML or JSON parsers. Avoid unrelated formatting changes.

## Docker Patterns

- Prefer multi-stage Dockerfiles where appropriate
- Base images typically from `intel/` registry or `openvino/` images
- `COPYLEFT_SOURCES` build arg pattern for including source of copyleft dependencies
- `.env` files for credentials and configuration (never commit secrets)
- Device mounting patterns: `/dev/dri` (GPU), `/dev/accel` (accelerators)
- Image tagging: `REGISTRY/component:TAG` with pinned version tags (for example `2026.1.0`) in production; avoid `:latest`

## CI/CD Workflows

- GitHub Actions in [.github/workflows/](workflows/)
- [fedaero.yaml](workflows/fedaero.yaml) packages components and publishes releases; component path filters select PR builds
- Common patterns: path filtering with `dorny/paths-filter`, pinned action SHAs, `persist-credentials: false`
- [fedaero-scan.yaml](workflows/fedaero-scan.yaml) runs Trivy filesystem/SBOM scanning, Bandit, and ShellCheck
- [zizmor-scan.yaml](workflows/zizmor-scan.yaml) checks GitHub Actions security; [skill-scan.yaml](workflows/skill-scan.yaml) scans and validates skills
- [dependabot.yml](dependabot.yml) configures dependency updates; keep configured paths aligned with manifests in this repository

## License Conventions

**All generated source files must include both lines below** using language-appropriate comment syntax:

```
# SPDX-FileCopyrightText: (C) <YEAR> Intel Corporation
# SPDX-License-Identifier: Apache-2.0
```

- Use the current year. Preserve the exact SPDX field names — do not paraphrase them.
- For file types that cannot contain comments (for example, binaries), use REUSE-compliant metadata in the component `LICENSES/` directory.

## Agent Behavior

### Scope
- **Stay within the component being asked about.** Do not modify other components or shared repository configuration unless the task requires it.
- Before editing, read the target file and the local `README.md`. If a local `AGENTS.md` exists, treat it as the authoritative override for that component.
- Do not add features, refactor code, introduce new dependencies, or change APIs beyond what was directly requested.

### Context management
- Load only files relevant to the current task; do not speculatively read unrelated components.
- Prefer `make help` to discover available targets rather than listing all Makefiles.
- Prefer targeted `grep`/search over reading entire directories.

## Key Conventions

1. **Environment variables** drive configuration — use `.env` files, never hardcode secrets
2. **Intel GPU support** — code should handle GPU/CPU/NPU device selection via env vars (e.g., `VLM_TARGET_DEVICE`)
3. **OpenVINO** is the inference runtime — models are typically in IR format (.xml/.bin)
4. **OVMS** (OpenVINO Model Server) serves models via OpenAI-compatible API
5. **Docker is required for application deployment** — use the active component's documented setup and deployment targets
6. **Security scanning** is mandatory — preserve the Trivy, Bandit, ShellCheck, workflow, and skill checks configured in CI
7. **No `:latest` tags** in production Docker image references — pin versions
8. **Proxy awareness** — many scripts support `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`
9. **Shell strict mode** — use `set -euo pipefail` for Bash scripts (`#!/usr/bin/env bash`), and POSIX-safe strict mode (`set -eu`) for `sh` scripts
10. **HuggingFace models** — follow the component's documented download procedure; pass `HF_TOKEN` through the environment when authentication is required

## Security

### General Guardrails (Always-On)

- Prefer least privilege across code, services, identities, file permissions, APIs, containers, and workflows.
- Treat all external input as untrusted; validate format, type, range, and length at trust boundaries.
- Never hard-code secrets, credentials, keys, tokens, or passwords anywhere; use environment variables only.
- Avoid exposing sensitive data in logs, traces, errors, metrics, or test artifacts.
- Prefer trusted, actively maintained dependencies and images; pin versions.
- Do not suggest bypassing or weakening existing security checks or validations.
- Fail safely and visibly; be explicit about assumptions and limitations.

### Codebase-Specific Guardrails

**Python**
- Never use `subprocess(..., shell=True)` with any external or user-controlled input; always pass a list: `subprocess.run([cmd, arg], shell=False)`.
- Never use `eval()`, `exec()`, or `pickle.loads()` on untrusted data.
- In HTTP services, validate request bodies and parameters against explicit schemas before processing; never trust raw dictionaries or unvalidated JSON.
- Use `secrets` module or environment variables for token generation; never use `random` for security-sensitive values.

**Docker / Containers**
- All Dockerfiles must include a non-root `USER` directive before the final `CMD`/`ENTRYPOINT`.
- Never use `--privileged`, `--cap-add=ALL`, or `--network=host` in Compose or runtime commands without explicit justification.
- Never use `pip install --trusted-host`, `--no-verify`, or unauthenticated `--index-url` sources.
- Never log, print, or `echo` `.env` file contents; never commit `.env` files.

**Shell scripts**
- Never construct shell commands by interpolating unvalidated variables; quote all variables: `"$var"`.
- Use `set -euo pipefail` (bash) or `set -eu` (sh); never use `set +e` to swallow errors silently.

**CI/CD**
- Pin all GitHub Actions to full commit SHAs, not tags.
- Never use `persist-credentials: true` unless required; scope tokens to minimum permissions.

### AI Output Trust Model

Treat AI-generated output as **untrusted draft code** until reviewed and tested.
Reject suggestions that bypass security controls for convenience or introduce unsafe defaults.

Load [`./skills/security-review/SKILL.md`](./skills/security-review/SKILL.md) when changes touch authentication, authorization, input parsing, secrets, Dockerfiles, Helm charts, or CI workflows.

## Contributing

See [`CONTRIBUTING.md`](../CONTRIBUTING.md) for PR guidelines and commit signing requirements.
For component-only sparse checkout, use paths relative to this repository root, as shown in the [UAV SDK README](../uav-mission-compute-sdk/README.md).

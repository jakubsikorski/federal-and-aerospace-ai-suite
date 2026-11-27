<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Installation on Ubuntu

This guide installs the Handheld (Soldier System) Blueprint on an Ubuntu 24.04 target using
the [Open Edge Platform (OEP) CLI Installer](https://github.com/open-edge-platform/edge-ai-libraries/tree/release-2026.3.0/frameworks/oep-installer-cli)
(`openedge-cli`). It is a single self-contained shell script that installs the Intel Edge Pack
components and the Federal and Aerospace Handheld (Soldier System) Blueprint without the need
for manual OS image build.

For the image-build and bootable-USB provisioning path instead, see
[Customized Image](./customized-image.md) and [Install OEP SDKs](install-oep-sdks.md).

For more detailed information on Edge Pack, refer to
[Edge Pack documentation](https://docs.openedgeplatform.intel.com/2026.3/platform-software/edge-pack/index.html).

## Prerequisites

- Ubuntu 24.04 LTS target with Internet access (or a configured proxy).
- Sudo access on the target. The installer prompts for the sudo password unless
  `SUDO_PASSWORD` or `SUDO_ASKPASS` is set, or passwordless sudo is granted.

## Step 1: Install the Edge Pack Components

Optionally, export a Hugging Face token. The configured models are public and do not require it; a token only helps when Hugging Face rate-limits anonymous downloads. Do not store the token in configuration files or commit it to version control.

```bash
export HF_TOKEN=hf_...
```

Edge Pack provides the platform-level services (EP-IPU, EP-Manageability, and EP-TSN) that the
suite blueprint runs on. Pass `--edgepack` when installing the blueprint to install them first:

```bash
curl -fsSL https://raw.githubusercontent.com/open-edge-platform/edge-ai-libraries/refs/heads/release-2026.3.0/frameworks/oep-installer-cli/rendered/openedge-cli > /tmp/openedge-cli
sudo chmod +x /tmp/openedge-cli
sudo mv /tmp/openedge-cli /usr/bin
openedge-cli install handheld_blueprint --edgepack
```

After the first invocation, the installer saves itself under `/usr/bin/openedge-cli`, so
subsequent commands can call `openedge-cli` directly.

During installation, system might reboot. After it is back online, follow instructions from installer to resume installation. If system didn't reboot or installation is done after reboot, go to [Application Installation step](../deploy-applications.md).

## Removing Components

Remove a profile or a component. This does not remove dependency items such as GPU/NPU drivers
installed by `--edgepack`; pass `--remove-all` to force removing everything:

```bash
openedge-cli remove federal_and_aerospace_ai_suite_handheld
openedge-cli remove federal_and_aerospace_ai_suite_handheld --remove-all
```

## Related Guides

- [OEP CLI Installer README](https://github.com/open-edge-platform/edge-ai-libraries/blob/release-2026.3.0/frameworks/oep-installer-cli/README.md) — full command reference, options, and credentials handling.
- [Customized Image](./customized-image.md) — OS image build and bootable-USB provisioning path.
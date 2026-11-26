<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Infrastructure Setup

There are 2 ways to prepare the infrastructure needed by the UAV applications:

1. **Edgepack on Ubuntu** — on an existing Ubuntu 24.04 system, install the Edge Pack and
   the applications with the OEP CLI Installer. See
   [Installation on Ubuntu](./infrastructure/installation-on-ubuntu.md).
2. **Customized image** — build a customized Ubuntu ISO with the Image Composer Tool,
   write it to a bootable USB, and install it on the target. See
   [Customized Image](./infrastructure/customized-image.md).

For the hardware and software features the provisioned platform provides, see
[Infrastructure Capabilities](./infrastructure/platform-capabilities.md). For common
problems, see the [Troubleshooting guide](./infrastructure/troubleshooting.md).

<!--hide_directive
:::{toctree}
:hidden:

System Requirements <./get-started/system-requirements.md>
Customized Image <./infrastructure/customized-image.md>
Infrastructure Capabilities <./infrastructure/platform-capabilities.md>
Troubleshooting <./infrastructure/troubleshooting.md>

:::
hide_directive-->

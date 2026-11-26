<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Customized Image (Using Image Composer Tool)

The [Image Composer Tool (ICT)](https://docs.openedgeplatform.intel.com/dev/platform-software/image-composer-tool/index.html)
is a command-line tool for building custom Linux images from pre-built packages.
To get a bootable RAW or ISO image, you define the target OS, packages, kernel, and disk layout.
ICT supports multiple distributions including Ubuntu, Azure Linux, and Red Hat compatible
distros on x86_64.

> [!NOTE]
> This path is intended for advanced users who need fine-grained control over disk
> layout, installed packages, and package repositories. Most users can start with the simpler
> path, [using standard Ubuntu image](./installation-on-ubuntu.md).

This article will show you how to:

- [Build OS image using default template](#build-os-image-using-default-template)
- [OS installation](#os-installation)
- [Package curation and template customization](#package-curation-and-template-customization)
- [Troubleshooting](#troubleshooting)

## Build OS image using default template

### Clone the repositories

```bash
git clone -b main https://github.com/open-edge-platform/image-composer-tool.git
```

Now, you should have the source code available in the `image-composer-tool` directory in your workspace (for example, `/home/user`).

```bash
export ICT_HOME=$(pwd)/image-composer-tool
```

### Build the tool

Produces `./image-composer-tool` in the repo root:

```bash
cd "$ICT_HOME"
go build -buildmode=pie -ldflags "-s -w" ./cmd/image-composer-tool
go build -buildmode=pie -o ./build/live-installer ./cmd/live-installer
```

### Install image composition prerequisites

These packages are required before composing any image:

```bash
sudo apt install systemd-ukify mmdebstrap
```

Follow the instructions at [Image Composition Prerequisites](https://docs.openedgeplatform.intel.com/dev/image-composer-tool/get-started/installation.html#image-composition-prerequisites) if you face issues installing packages using apt.

> [!NOTE]
> `mmdebstrap` version 0.8.x (shipped with Ubuntu OS version 22.04) has known
> issues. Ensure you have version 1.4.3 or later. On Ubuntu OS version 23.04 or later, the
> repository version is sufficient.

### Configure the template

The default template location is `$ICT_HOME/image-templates/ubuntu24` or `$ICT_HOME/image-templates/ubuntu26`, depending on the OS of your choosing. Select one of the `edgepack` templates, as it provides the best developer experience: it comes with all Intel drivers preinstalled.

```bash
export TEMPLATE="$ICT_HOME/image-templates/ubuntu24/ubuntu24-x86_64-edgepack-unattended-iso.yml"
```

In `$TEMPLATE`, set the values for `users.name` and `users.password` as desired.
The password must contain a SHA-512 hash generated using one of the following tools:

```bash
# Using openssl (requires `openssl` to be installed)
openssl passwd -6 'your-password-here'

# Using mkpasswd (requires `whois` to be installed)
mkpasswd --method=sha-512 'your-password-here'
```

Adapt this template to suit your use case. The advanced customization options are discussed in the [Package curation and template customization](#package-curation-and-template-customization) section below.

### Validate the template

Check the template for syntax and schema errors before starting a full
build (fast, no root required):

```bash
./image-composer-tool validate "$TEMPLATE"
```

### Build the image

Run the build with elevated privileges so that the tool can manage loop devices
and chroot environments. Pass `-E` to preserve your proxy and environment variables:

```bash
sudo -E ./image-composer-tool build "$TEMPLATE"
```

### Build output

When the build completes, expect the following output on the console with build timings:

```bash
2026-11-20T10:47:56.152+0100	INFO	display/display.go:57	╔════════════════════════════════════════════════════════════════════════════╗
2026-11-20T10:47:56.152+0100	INFO	display/display.go:58	║                    ✓ IMAGE CREATED SUCCESSFULLY                            ║
2026-11-20T10:47:56.152+0100	INFO	display/display.go:59	╚════════════════════════════════════════════════════════════════════════════╝
2026-11-20T10:47:56.152+0100	INFO	display/display.go:63	  Image Type:   ISO
2026-11-20T10:47:56.152+0100	INFO	display/display.go:65	  Generated Artifacts (including SBOM):
2026-11-20T10:47:56.152+0100	INFO	display/display.go:83	    • edgepack-os-image-ubuntu-unattended-24.04.iso (2.74 GB)
2026-11-20T10:47:56.152+0100	INFO	display/display.go:84	      /home/user/image-composer-tool/workspace/ubuntu-ubuntu24-x86_64/imagebuild/Default_ISO/edgepack-os-image-ubuntu-unattended-24.04.iso
2026-11-20T10:47:56.185+0100	INFO	image-composer-tool/build.go:466	image build completed successfully
2026-11-20T10:47:56.185+0100	INFO	display/display.go:164	  Build Timings:
2026-11-20T10:47:56.185+0100	INFO	display/display.go:169	  | Total Time                       | 7m53.05s       |
```

The output artefacts are written to:

```text
./workspace/ubuntu-ubuntu24-x86_64/imagebuild/<config-name>/
```

Expected artifact:

| File                                            | Description                                   |
| ----------------------------------------------- | --------------------------------------------- |
| `edgepack-os-image-ubuntu-unattended-24.04.iso` | Unattended installation ISO (ready to flash)  |

## OS installation

After the ISO is created, use your preferred method (such as a USB stick or KVM) to install it on
the target system. The following steps write the image to a USB stick.

Run `lsblk` to see how the USB stick is mapped on the local filesystem. In this example it is `/dev/sdb`:

```bash
lsblk
```

Erase the filesystem signatures on the USB stick and write the ISO. Replace `sdb` with the device
reported by `lsblk` if it is different — the target device is overwritten:

```bash
sudo wipefs --all /dev/sdb
sudo dd if="$ICT_HOME/workspace/ubuntu-ubuntu24-x86_64/imagebuild/Default_ISO/edgepack-os-image-ubuntu-unattended-24.04.iso" of=/dev/sdb bs=16M status=progress oflag=sync
```

Remove the USB stick, connect it to the target system, open the BIOS settings, and select the USB
stick under **Boot → Boot options**. Save the changes and reboot; the automatic installation starts.

After the OS is installed, install the applications with `openedge-cli`:

- [Install UAV Vision Analytics on Ubuntu](./installation-on-ubuntu.md)
- [Install UAV Ground Station on Ubuntu](../uav-ground-station/installation-on-ubuntu.md)

## Package curation and template customization

This section explains how to curate package lists with the `update-install-packages` skill and produce a new Image Composer Tool (ICT) image variant on top of the default template.

Use this flow when you want to build a custom image flavor (for example, debug, media-heavy, or minimal runtime) without editing the baseline files manually each time.

### End-to-end flow

1. Start from the `image-composer-tool` repository root and define your package delta (add or delete).
2. Run the `update-install-packages` skill to apply package curation safely.
3. Validate YAML and backups created by the skill.
4. Copy the edgepack ICT template into a working template for your variant.
5. Validate and build the image using ICT.
6. Record artifact path and package delta for reproducibility.

### Run the skill

If you are using Copilot Chat in agent mode, invoke the skill with a natural language prompt describing your intent. For example:

```text
Add htop, jq, and iperf3 to the ict-template
```

```text
Delete mosquitto and mosquitto-clients from the ict-template.
```

```text
Add sysbench and stress-ng to the ict-template for a debug image variant.
```

The skill is expected to:

- validate package name format
- verify package availability in Ubuntu 24.04 repositories
- optionally search repositories for packages matching hardware details (device name, model, or vendor) and confirm matches before adding
- create backups before file changes
- return per-file package change results (`added`, `deleted`, `already-present`, `not-found`)
- validate shell and YAML syntax after updates

### Build an ICT variant from the curated baseline

After package curation succeeds, create a variant template from the default template:

```bash
cp "$TEMPLATE" \
   "$(dirname "$TEMPLATE")/my-template.yml"
```

Validate and build this variant using the same commands as the default template (see [Validate the template](#validate-the-template) and [Build the image](#build-the-image) above), substituting `my-template.yml` for `$TEMPLATE`. See [Troubleshooting](#troubleshooting) below if you run into issues.

Expected output artifact type:

- unattended installation ISO (`.iso`)

### Safety and rollback

Follow these rules for reliable curation:

- do not edit the only source copy without backup
- stop on any precondition or YAML validation failure
- restore from backup if update or validation fails
- do not request or store secrets in prompts or scripts

If rollback is needed, restore backup files produced by the skill for each modified target file and re-run validation.

## Troubleshooting

### Package not found or conflicting versions

If the build fails with errors like `failed: bad status: 404 Not Found` or conflicting versions, the package may not exist in the configured repositories or may have been renamed in Ubuntu 24.04.

1. Confirm the package name is correct:

   ```bash
   apt-cache search <name>
   apt-cache show <name>
   ```

2. Clean the ICT cache and temporary files, then rebuild:

   ```bash
   sudo ./image-composer-tool cache clean
   sudo rm -rf tmp/
   ```

### Mirror issues

Standard Ubuntu mirrors may occasionally be unreliable or return stale metadata. If you encounter intermittent download failures or hash-sum mismatches during the build, update the `packageRepositories` section in your template to use other open-source mirrors. For example, using the Kernel.org mirror:

```yaml
packageRepositories:
  - codename: "noble"
    url: "http://mirrors.edge.kernel.org/ubuntu/"
    component: "main restricted universe multiverse"
    priority: 500
```

To find the fastest mirror for your region, you can optionally use `mirrorselect`:

```bash
sudo snap install mirrorselect
mirrorselect --country us
```

Replace `us` with your country code (for example, `de`, `in`, `sg`) and use the returned URL in `packageRepositories`.

After updating the mirror, clean and rebuild:

```bash
sudo ./image-composer-tool cache clean
sudo rm -rf tmp/
```

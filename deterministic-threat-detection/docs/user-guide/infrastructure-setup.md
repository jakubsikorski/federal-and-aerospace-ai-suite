# Infrastructure Setup

Use the OEP CLI Installer to install or remove Deterministic Threat Detection on an existing
Ubuntu 24.04 system. Run the install, remove and start steps on machine-1 only: `make up`
copies what the other machines need. For the full installer documentation, see the
[OEP CLI Installer guide](https://docs.openedgeplatform.intel.com/dev/OEP-articles/oep-cli-installer/index.html).

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/open-edge-platform/edge-ai-libraries/refs/heads/release-2026.3.0/frameworks/oep-installer-cli/rendered/openedge-cli-federal-aerospace | bash -s -- install deterministic_threat_detection
```

## Remove

```bash
openedge-cli remove deterministic_threat_detection
```

## Start the Application

The installer places the application in
`$HOME/.local/open-edge-platform/openedge-cli/installed/deterministic_threat_detection`. Go to that
directory and follow the steps in the regular documentation:

```bash
cd $HOME/.local/open-edge-platform/openedge-cli/installed/deterministic_threat_detection
```

- [System Requirements](./get-started/system-requirements.md) for the host, TSN and switch preparation.
- [Get Started](./deterministic-threat-detection/get-started.md) to start and stop the application.




<!--hide_directive
:::{toctree}
:hidden:

System Requirements <./get-started/system-requirements.md>

:::
hide_directive-->

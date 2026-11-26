<!--hide_directive
```{eval-rst}
.. meta::
   :description: Learn how a Time-Sensitive Networking (TSN) switch keeps AI video workloads deterministic while the network is flooded with background traffic.
   :keywords: mission sensing service, deterministic threat detection, Time-Sensitive Networking, TSN, IEEE 802.1Qbv, Time-Aware Shaper
```
hide_directive-->

# Mission Sensing Service Blueprint

<!--hide_directive
<div class="component_card_widget">
  <a class="icon_github" href="https://github.com/open-edge-platform/federal-and-aerospace-ai-suite/tree/main/deterministic-threat-detection">
     GitHub
  </a>
  <a class="icon_document" href="https://github.com/open-edge-platform/federal-and-aerospace-ai-suite/blob/main/deterministic-threat-detection/README.md">
     Readme
  </a>
  <a class="icon_download" href="https://github.com/open-edge-platform/federal-and-aerospace-ai-suite/releases/download/fedaero-latest/deterministic-threat-detection.zip">
     Download Package
  </a>
</div>
hide_directive-->

Mission Sensing Service Blueprint demonstrates how a Time-Sensitive Networking (TSN) switch keeps
AI video workloads deterministic while the network is flooded with background traffic. This
blueprint is a preview.

The blueprint's main component is the Deterministic Threat Detection application. It uses an
IEEE 802.1Qbv Time-Aware Shaper (TAS), which confines the background load to its own time
slot, so the camera traffic keeps its low latency and the tracking quality remains stable.
A single web dashboard runs two use cases:

- **Latency:** two cameras run AI inference and publish the results over MQTT. The dashboard
  shows the one-way network latency per message.
- **Tracking:** Scenescape fuses two camera views into tracked people. The dashboard shows the
  live Higher Order Tracking Accuracy (HOTA) score for test videos or the tracker rate for
  Basler cameras.

A slider on the dashboard injects User Datagram Protocol (UDP) load of up to 1 Gbit/s from a
fourth machine, and a toggle enables the TAS on the switch. With the shaper off, the load starves
both use cases. With it on, they are unaffected.

The setup requires four Linux machines and a Moxa TSN switch. For details, see
[System Requirements](./get-started/system-requirements.md).



## Installation Guide

Follow the steps below to set up and run the blueprint:

1. [Infrastructure Setup](./infrastructure-setup.md) — Install the application with the OEP CLI Installer.
2. [Deterministic Threat Detection](./deterministic-threat-detection/index.md) — Prepare the machines and the switch, then start the application.
3. [Edge Workloads and Benchmarks Guide](./benchmarking/run-edge-benchmarks.md) — Measure the platform performance of the edge node.

<!--hide_directive
:::{toctree}
:hidden:

Infrastructure Setup <./infrastructure-setup.md>
Deterministic Threat Detection <./deterministic-threat-detection/index.md>
Benchmarks <./benchmarking/run-edge-benchmarks.md>
AI Agent SKILLs <agents.md>
Release Notes <./release-notes.md>

:::
hide_directive-->

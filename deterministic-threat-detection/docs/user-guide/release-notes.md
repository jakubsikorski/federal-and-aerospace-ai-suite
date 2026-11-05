# Release Notes: Deterministic Threat Detection

- [Version 1.1.0](#version-110)
- [Version 1.0.1](#version-101)
- [Version 1.0.0](#version-100)

## Version 1.1.0

**New**:

- One web dashboard for both use cases: live latency, tracking quality (HOTA), network-load slider,
  Qbv toggle, system utilization and a live architecture view.
- One command deployment for four machines: `make up` / `make down`.
- Basler GigE camera support with PTP hardware timestamps: `make up SOURCE=basler`.
- Setup scripts for the TSN NIC, VLANs and PTP profiles (`src/setup/`).

**Changed**:

- SceneScape integration uses a single `gvapython` adapter instead of a patched SceneScape
  checkout; SceneScape 2026.3 images are used unmodified.
- Documentation rewritten around the new design. The AXIS RTSP camera, sensor producer and MQTT
  aggregator use case and the offline HOTA scripts were removed.

## Version 1.0.1

**Release Date**: June 25, 2026

**New**:

- The deterministic threat detection subproject with Intel® Scenescape-specific changes
  and a use case for testing purposes.

- Documentation for setting up, configuring, and capturing HOTA metrics with
  Intel Scenescape, both with and without traffic shaping.

**Improved**:

- Restructured the documentation to separate the Intel Scenescape-specific
  content into its own section, while keeping the original RTSP use case
  documentation intact.

## Version 1.0.0

**Release Date**: March 25, 2026

**New**:

- Initial release of the deterministic threat detection subproject: a real-time threat detection
  system for time-sensitive networks, the code to test the use case, and documentation to set up,
  configure and run it.

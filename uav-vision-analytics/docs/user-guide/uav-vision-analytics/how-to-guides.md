# How To Use UAV Vision Analytics

These guides cover what the application can do and how to operate it as
shipped: viewing streams, running the ground station, deployment modes,
testing, and troubleshooting. To change what the application does —
cameras, models, pipelines, overlay, search tuning — see
[How to Customize](./how-to-customize.md).

## Ground Station

- [View the UAV Video Stream in QGroundControl](./how-to-guides/qgroundcontrol.md) — Configure QGroundControl
  to receive the UAV Vision Analytics video stream
- [Monitor Multiple UAVs in the Ground Station](./how-to-guides/multi-uav-ground-station.md) —
  Connect multiple UAVs, per-UAV tabs, and the per-host Flight Summary
- [Search Live Video with Natural Language](./how-to-guides/vss-live-search.md) —
  How live indexing and UAV/camera-scoped search work in the ground station

## Deployment and Operations

- [Switch Between Standalone and UAV Mission Compute SDK Modes](./how-to-guides/switch-deployment-mode.md) —
  Architecture differences and the `HOST_IP` binding requirement
- [Run Object Detection on the NPU](./how-to-guides/enable-npu-inference.md) — NPU
  enablement end-to-end, including common failure modes
- [Keep PX4 SITL Armed During Pipeline Testing](./how-to-guides/simulate-continuous-mission.md) —
  Keep PX4 SITL armed during interactive pipeline testing
- [Read and Filter Detection Logs](./how-to-guides/view-detection-logs.md) —
  Tail, filter, and correlate the per-pipeline detection metadata

## Reference

- [Makefile Target Reference](./how-to-guides/makefile.md) — Shorthand targets for
  model setup, stack management, and pipeline control
- [Troubleshoot UAV Vision Analytics](./troubleshooting.md) — Common issues and
  fixes for deployment, inference, and streaming problems

<!--hide_directive
:::{toctree}
:hidden:

View With QGroundControl <./how-to-guides/qgroundcontrol.md>
Monitor Multiple UAVs in Ground Station <./how-to-guides/multi-uav-ground-station.md>
Search Live Video with VSS <./how-to-guides/vss-live-search.md>
Switch Deployment Modes <./how-to-guides/switch-deployment-mode.md>
Run NPU Inference <./how-to-guides/enable-npu-inference.md>
Simulate Continuous Mission <./how-to-guides/simulate-continuous-mission.md>
View Detection Logs <./how-to-guides/view-detection-logs.md>
Makefile Reference <./how-to-guides/makefile.md>
:::
hide_directive-->

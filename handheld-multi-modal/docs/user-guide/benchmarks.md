<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Benchmark — Handheld (Soldier System) Blueprint

For platform-level benchmarking — vision AI inference, hardware-accelerated media processing,
end-to-end video analytics pipelines, and generative AI — see the
[Edge Workloads and Benchmarks Guide](./benchmarking/run-edge-benchmarks.md).

The Handheld Multi-Modal application itself reports live CPU, memory, GPU, NPU, and power
telemetry in the **Resources** panel of the operator console. See the
[Operator Guide](operator-guide.md#resources).

The **Performance** tab next to Resources shows time to alert, time to spoken answer, smooth
cameras, approximate answer speed, and peak memory over the last 5 minutes, plus one tile
each for Chat Q&A, Audio Analyzer, and Text To Speech. It describes the values without
grading them against targets. See the
[Performance KPI Plan](./benchmarking/handheld-performance-kpi-plan.md) for definitions,
the implemented measurements, and the upstream service requests.

<!--hide_directive
:::{toctree}
:hidden:

Edge Workloads and Benchmarks Guide <./benchmarking/run-edge-benchmarks.md>
Performance KPI Plan <./benchmarking/handheld-performance-kpi-plan.md>
:::
hide_directive-->

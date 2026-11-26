# UAV Ground Station

<!--hide_directive
<div class="component_card_widget">
  <a class="icon_github" href="https://github.com/open-edge-platform/federal-and-aerospace-ai-suite/tree/main/uav-ground-station">
     GitHub
  </a>
  <a class="icon_document" href="https://github.com/open-edge-platform/federal-and-aerospace-ai-suite/blob/main/uav-ground-station/README.md">
     Readme
  </a>
  <a class="icon_download" href="https://github.com/open-edge-platform/federal-and-aerospace-ai-suite/releases/tag/fedaero-latest">
     Download Package
  </a>
</div>
hide_directive-->

UAV Ground Station is a browser dashboard that runs on a separate machine and shows the live video,
scene captions, and host utilization of one or more UAV Vision Analytics hosts. It can optionally add
Live Natural Language Search over the camera footage, and it builds Flight Summary reports from the
scene captions.

Pair it with a UAV host running the
[UAV Vision Analytics](../uav-vision-analytics/index.md)
application.

## Documentation

- [Installation on Ubuntu](./installation-on-ubuntu.md): install and start the ground station on Ubuntu 24.04
  with the OEP CLI Installer.
- [Get Started](./get-started.md): deploy the ground station, connect a UAV host, and use VSS search,
  multiple UAVs, and Flight Summary.
- [Configuration](./configuration.md): every `.env` variable and the backend route reference.
- [Release Notes](./release-notes.md): what each version adds, with known issues.

<!--hide_directive
:::{toctree}
:hidden:

Installation on Ubuntu <./installation-on-ubuntu.md>
Get Started <./get-started.md>
Configuration <./configuration.md>
Release Notes <./release-notes.md>

:::
hide_directive-->

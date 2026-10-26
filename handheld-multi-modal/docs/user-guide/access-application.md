# Access Application User Interface

The operator console is served by the NGINX TLS reverse proxy at
<https://localhost:8443/>. Every published port of the application binds to `127.0.0.1` only
and is not exposed on any external IP address. Since the intended use is on handheld
devices, the console does not provide authentication or authorization.

> [!NOTE]
> The "self-signed certificate" browser warning is expected until the certificate is trusted.
> Modern browsers require HTTPS to enable the microphone used for voice questions, therefore
> the NGINX reverse proxy uses a certificate generated on the device for `localhost` and
> `127.0.0.1`.

To add the certificate to the trust pool, open a browser of your choice and navigate to
`Settings->Certificates->Manage certificates` page. Next, select the `Authorities` tab and
click `Import`. In file explorer, navigate to the folder that contains the Handheld Multi-Modal
application and select the `config/tls/server.crt` file. Finally, select the
`Trust this CA to identify websites` checkbox and click `Ok`.

Do not copy another device's TLS keys. If a certificate from an older version names a LAN
address, delete `config/tls/server.*` and run `make demo-up` (or `make up`) again.

| Service | URL | Notes |
| ------- | --- | ----- |
| Operator console | <https://localhost:8443/> | Live cameras, evidence, assistant, mission, alerts, voice, and resources |
| Architecture page | <https://localhost:8443/architecture.html> | Interactive diagram of every service, model placement, and API used |

The proxy answers `421` to other host names and `403` to a foreign `Origin`, so open the console
as `localhost:8443` or `127.0.0.1:8443`. Run the browser full screen (F11 or kiosk mode) on the
handheld. After deploying a new version, reload an already open browser tab once to load the
updated client.

## Remote Access Through an SSH Tunnel

To view the console from a laptop, forward the UI and TURN ports over SSH:

```bash
ssh -N -L 8443:127.0.0.1:8443 -L 3478:127.0.0.1:3478 <user>@<device-address>
```

Then open <https://localhost:8443/> on the laptop. Keep both local port numbers: the proxy
accepts only `localhost:8443` origins, and live video is relayed over TURN/TCP on port 3478
because SSH does not carry UDP.

If the page loads but camera tiles stay black:

- check the `ssh` output for `bind ... Address already in use` on port 3478;
- on the laptop, `nc -vz 127.0.0.1 3478` (Windows: `Test-NetConnection 127.0.0.1 -Port 3478`)
  must succeed;
- `chrome://webrtc-internals` (Firefox: `about:webrtc`) must show a `relay` candidate; managed
  browser policies can block it.

> [!WARNING]
> The tunnel grants full, unauthenticated access to the console. Do not share the forwarded
> ports (`ssh -g`, `GatewayPorts`) or forward them from a shared machine.

## Adding Cameras

`make up` starts without cameras. Open **Cameras** in the top bar of the console to add:

- a **network camera** by its `rtsp://` address, or
- a **publisher on this device** that sends H.264 RTSP video to the loopback-only address the
  console shows, for example `rtsp://127.0.0.1:8554/cam3`.

For example, publish a local video file as a camera with FFmpeg:

```bash
ffmpeg -re -stream_loop -1 -i video.mp4 -an -c:v libx264 -preset veryfast -tune zerolatency \
  -f rtsp -rtsp_transport tcp rtsp://127.0.0.1:8554/cam3
```

The console shows the same command, with the right address, after a publisher camera is added.

The [`examples/realsense-rtsp.sh`](https://github.com/open-edge-platform/federal-and-aerospace-ai-suite/blob/main/handheld-multi-modal/examples/realsense-rtsp.sh)
script publishes an Intel® RealSense™ camera the same way.

See the [Operator Guide](operator-guide.md) for a description of every part of the console.

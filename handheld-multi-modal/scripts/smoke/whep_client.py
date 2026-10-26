"""Minimal receive-only WHEP smoke; requires decoded video, not just SDP."""

import argparse
import asyncio
import functools
import re
import ssl
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen


CONTEXT = None
# MediaMTX WHEP OPTIONS advertises ICE servers as Link headers.
LINK = re.compile(r'^<(.+?)>; rel="ice-server"(?:; username="(.+?)"; credential="(.+?)"; credential-type="password")?')


def request(url, method, body=None):
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"WHEP URL must be http(s): {url}")
    headers = {"Content-Type": "application/sdp"} if body is not None else {}
    with urlopen(Request(url, data=body, headers=headers, method=method), timeout=10, context=CONTEXT) as response:  # nosec B310 - scheme checked above
        return response.status, response.headers, response.read()


def relay_ice_servers(endpoint):
    from aiortc import RTCIceServer

    _, headers, _ = request(endpoint, "OPTIONS")
    servers = []
    for link in headers.get_all("Link") or []:
        match = LINK.match(link)
        if match and match[1].startswith("turn:"):
            servers.append(RTCIceServer(match[1], match[2], match[3]))
    if not servers:
        raise RuntimeError("WHEP OPTIONS advertised no TURN server")
    return servers


async def smoke(endpoint, relay_only=False):
    from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription

    ice_servers = []
    if relay_only:
        # Mimic a browser that reaches the device only through an SSH tunnel:
        # gather relay candidates only, from the advertised TURN server.
        import aioice
        import aiortc.rtcicetransport as ice_transport

        ice_servers = await asyncio.to_thread(relay_ice_servers, endpoint)
        ice_transport.Connection = functools.partial(
            aioice.Connection, transport_policy=aioice.TransportPolicy.RELAY
        )
    peer = RTCPeerConnection(RTCConfiguration(iceServers=ice_servers))
    peer.addTransceiver("video", direction="recvonly")
    receiver = None
    video_ready = asyncio.Event()
    session = None

    async def receive(track):
        for _ in range(3):
            frame = await track.recv()
            if frame.width <= 0 or frame.height <= 0:
                raise RuntimeError("WHEP decoded an invalid video frame")

    @peer.on("track")
    def on_track(track):
        nonlocal receiver
        if track.kind == "video":
            if receiver is not None:
                raise RuntimeError("Unexpected second video track")
            receiver = asyncio.create_task(receive(track))
            video_ready.set()

    try:
        offer = await peer.createOffer()
        await peer.setLocalDescription(offer)
        status, headers, answer = await asyncio.to_thread(
            request, endpoint, "POST", peer.localDescription.sdp.encode()
        )
        location = headers.get("Location")
        if status != 201 or not location:
            raise RuntimeError(f"WHEP expected 201 and Location, got {status}")
        session = urljoin(endpoint, location)
        if urlsplit(session)[:2] != urlsplit(endpoint)[:2]:
            session = None
            raise RuntimeError("WHEP returned a cross-origin session URL")
        if headers.get_content_type() != "application/sdp":
            raise RuntimeError("WHEP response is not application/sdp")
        await peer.setRemoteDescription(RTCSessionDescription(answer.decode(), "answer"))
        await asyncio.wait_for(video_ready.wait(), timeout=10)
        if receiver is None:
            raise RuntimeError("WHEP did not negotiate a video track")
        await asyncio.wait_for(receiver, timeout=25)
        print("PASS: WHEP negotiated and decoded three video frames" + (" over TURN relay" if relay_only else ""))
    finally:
        if receiver is not None and not receiver.done():
            receiver.cancel()
            try:
                await receiver
            except asyncio.CancelledError:
                pass
        try:
            if session is not None:
                status, _, _ = await asyncio.to_thread(request, session, "DELETE")
                if status not in (200, 204):
                    raise RuntimeError(f"WHEP session cleanup returned {status}")
        finally:
            await peer.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("endpoint")
    parser.add_argument("--cafile", help="trust this certificate, e.g. config/tls/server.crt")
    parser.add_argument("--relay-only", action="store_true", help="use only the advertised TURN relay (SSH-tunnel path)")
    args = parser.parse_args()
    global CONTEXT
    CONTEXT = ssl.create_default_context(cafile=args.cafile) if args.cafile else None
    parsed = urlsplit(args.endpoint)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        parser.error("endpoint must be an HTTP(S) WHEP URL")
    asyncio.run(smoke(args.endpoint, args.relay_only))


if __name__ == "__main__":
    main()

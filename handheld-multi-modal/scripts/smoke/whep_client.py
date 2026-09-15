"""Minimal receive-only WHEP smoke; requires decoded video, not just SDP."""

import argparse
import asyncio
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen


def request(url, method, body=None):
    headers = {"Content-Type": "application/sdp"} if body is not None else {}
    with urlopen(Request(url, data=body, headers=headers, method=method), timeout=10) as response:
        return response.status, response.headers, response.read()


async def smoke(endpoint):
    from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription

    peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
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
        print("PASS: WHEP negotiated and decoded three video frames")
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
    args = parser.parse_args()
    parsed = urlsplit(args.endpoint)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        parser.error("endpoint must be an HTTP(S) WHEP URL")
    asyncio.run(smoke(args.endpoint))


if __name__ == "__main__":
    main()

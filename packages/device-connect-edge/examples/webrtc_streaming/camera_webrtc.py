#!/usr/bin/env python3
"""WebRTC Camera Device — produces a video stream over WebRTC,
signaled through device-connect RPCs.

Usage:
    export NATS_URL=nats://fabric.deviceconnect.dev:4222
    export NATS_CREDENTIALS_FILE=../beta/credentials/beta-device-001.creds.json
    python camera_webrtc.py [--device-id camera-001] [--fps 30]

Requires:
    pip install aiortc numpy

The camera registers on the mesh and waits for stream requests.
When another device calls the `start_stream` RPC, this device
creates a WebRTC offer with a test-pattern video track. The
caller sends back an SDP answer via `accept_answer`, completing
the handshake. Media then flows peer-to-peer over WebRTC — not
through NATS.
"""

import argparse
import asyncio
import fractions
import logging
import os
import sys
import time

try:
    import numpy as np
    from av import VideoFrame
    from aiortc import (
        MediaStreamTrack,
        RTCConfiguration,
        RTCIceServer,
        RTCPeerConnection,
        RTCSessionDescription,
    )
except ImportError as exc:
    print(f"Missing dependency: {exc}", file=sys.stderr)
    print("Install with:  pip install aiortc numpy", file=sys.stderr)
    sys.exit(1)

from device_connect_edge import DeviceRuntime
from device_connect_edge.drivers import DeviceDriver, rpc, emit
from device_connect_edge.types import DeviceIdentity, DeviceStatus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-24s  %(levelname)-7s  %(message)s",
)
logger = logging.getLogger("webrtc.camera")


# ── Test video source ────────────────────────────────────────────


class TestVideoTrack(MediaStreamTrack):
    """Generates an animated test-pattern video (colour-cycling gradient
    with a moving white bar). No camera hardware needed."""

    kind = "video"

    def __init__(self, width: int = 640, height: int = 480, fps: int = 30):
        super().__init__()
        self._w = width
        self._h = height
        self._fps = fps
        self._t0 = time.time()
        self._n = 0

    async def recv(self) -> VideoFrame:
        self._n += 1

        # Pace to target FPS
        target = self._t0 + self._n / self._fps
        delay = target - time.time()
        if delay > 0:
            await asyncio.sleep(delay)

        # Colour-cycling gradient background (BGR)
        hue = (self._n * 2) % 180
        img = np.empty((self._h, self._w, 3), dtype=np.uint8)
        img[:, :, 0] = np.linspace(hue, (hue + 60) % 256, self._w, dtype=np.uint8)
        img[:, :, 1] = 128
        img[:, :, 2] = np.linspace(255 - hue, (255 - hue + 90) % 256, self._w, dtype=np.uint8)

        # Moving white bar so it's obvious the stream is alive
        bx = (self._n * 4) % self._w
        img[:, max(0, bx - 4) : min(self._w, bx + 4), :] = 255

        frame = VideoFrame.from_ndarray(img, format="bgr24")
        frame.pts = self._n
        frame.time_base = fractions.Fraction(1, self._fps)
        return frame


# ── WebRTC config (STUN for NAT traversal) ───────────────────────

RTC_CONFIG = RTCConfiguration(
    iceServers=[RTCIceServer(urls=["stun:stun.l.google.com:19302"])]
)


# ── Camera driver ────────────────────────────────────────────────


class CameraStreamDriver(DeviceDriver):
    """Camera device that streams test-pattern video via WebRTC."""

    device_type = "camera"

    def __init__(self, fps: int = 30):
        super().__init__()
        self._fps = fps
        self._peers: dict[str, RTCPeerConnection] = {}

    @property
    def identity(self) -> DeviceIdentity:
        return DeviceIdentity(
            device_type="camera",
            manufacturer="Artly",
            model="WebRTC-Cam-1",
            firmware_version="0.1.0",
            description="Camera with WebRTC streaming (test pattern)",
        )

    @property
    def status(self) -> DeviceStatus:
        return DeviceStatus(
            location="lab",
            availability="busy" if self._peers else "idle",
        )

    # ── Signaling RPCs ───────────────────────────────────────────

    @rpc()
    async def start_stream(self, caller_device_id: str) -> dict:
        """Create a WebRTC offer for a requesting device.

        Returns the SDP offer. The caller must reply with accept_answer().

        Args:
            caller_device_id: Device ID of the stream consumer.
        """
        # Tear down any stale session with this peer
        if caller_device_id in self._peers:
            await self._close_peer(caller_device_id)

        pc = RTCPeerConnection(configuration=RTC_CONFIG)
        self._peers[caller_device_id] = pc

        # Attach the video track
        pc.addTrack(TestVideoTrack(fps=self._fps))

        # Log connection state transitions
        @pc.on("connectionstatechange")
        async def _on_state():
            state = pc.connectionState
            logger.info("[peer %s] connection state → %s", caller_device_id, state)
            await self.stream_state_changed(
                peer_device_id=caller_device_id, state=state,
            )
            if state in ("failed", "closed"):
                await self._close_peer(caller_device_id)

        # Create offer and gather ICE candidates
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)

        # Wait for ICE gathering (candidates are bundled into the SDP)
        for _ in range(50):
            if pc.iceGatheringState == "complete":
                break
            await asyncio.sleep(0.1)

        logger.info("Created offer for %s (%d bytes SDP)",
                     caller_device_id, len(pc.localDescription.sdp))

        return {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
        }

    @rpc()
    async def accept_answer(
        self, caller_device_id: str, sdp: str, type: str,
    ) -> dict:
        """Accept an SDP answer from the stream consumer.

        Args:
            caller_device_id: Device that sent the answer.
            sdp: SDP answer string.
            type: SDP type (should be 'answer').
        """
        pc = self._peers.get(caller_device_id)
        if not pc:
            return {"error": f"no pending offer for {caller_device_id}"}

        await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=type))
        logger.info("Accepted answer from %s — WebRTC handshake complete",
                     caller_device_id)
        return {"status": "connected"}

    @rpc()
    async def stop_stream(self, caller_device_id: str) -> dict:
        """Tear down the stream to a specific consumer.

        Args:
            caller_device_id: Device to disconnect.
        """
        closed = caller_device_id in self._peers
        await self._close_peer(caller_device_id)
        logger.info("Stopped stream to %s (was_active=%s)", caller_device_id, closed)
        return {"status": "stopped", "was_active": closed}

    @rpc()
    async def list_streams(self) -> dict:
        """List active outgoing WebRTC streams."""
        return {
            "streams": [
                {"peer": pid, "state": pc.connectionState}
                for pid, pc in self._peers.items()
            ],
        }

    # ── Events ───────────────────────────────────────────────────

    @emit()
    async def stream_state_changed(self, peer_device_id: str, state: str):
        """WebRTC connection state changed for a peer.

        Args:
            peer_device_id: The remote peer device.
            state: New connection state (new/connecting/connected/failed/closed).
        """
        pass

    # ── Internal ─────────────────────────────────────────────────

    async def _close_peer(self, device_id: str):
        pc = self._peers.pop(device_id, None)
        if pc:
            await pc.close()

    async def connect(self) -> None:
        logger.info("Camera driver ready  (device_id=%s, fps=%d)",
                     self._device_id, self._fps)

    async def disconnect(self) -> None:
        for pid in list(self._peers):
            await self._close_peer(pid)
        logger.info("Camera driver disconnected")


# ── Entrypoint ───────────────────────────────────────────────────


async def run(device_id: str = "camera-001", fps: int = 30):
    """Run the camera device standalone."""
    allow_insecure = os.getenv(
        "DEVICE_CONNECT_ALLOW_INSECURE", ""
    ).lower() in ("1", "true", "yes")

    device = DeviceRuntime(
        driver=CameraStreamDriver(fps=fps),
        device_id=device_id,
        allow_insecure=allow_insecure,
    )
    await device.run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WebRTC Camera Device")
    parser.add_argument("--device-id", default="camera-001",
                        help="Device ID to register as (default: camera-001)")
    parser.add_argument("--fps", type=int, default=30,
                        help="Frames per second for the test pattern (default: 30)")
    args = parser.parse_args()

    try:
        asyncio.run(run(device_id=args.device_id, fps=args.fps))
    except KeyboardInterrupt:
        sys.exit(0)

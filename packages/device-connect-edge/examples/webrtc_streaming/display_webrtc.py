#!/usr/bin/env python3
"""WebRTC Display Device — consumes a video stream over WebRTC,
signaled through device-connect RPCs.

Usage:
    # Passive mode (wait for trigger_stream.py to start a stream):
    export NATS_URL=nats://fabric.deviceconnect.dev:4222
    export NATS_CREDENTIALS_FILE=../beta/credentials/beta-device-002.creds.json
    python display_webrtc.py [--device-id display-001]

    # Auto-connect to a camera on startup:
    python display_webrtc.py --camera camera-001

Requires:
    pip install aiortc numpy

The display registers on the mesh. When `watch_camera` is called
(via --camera flag, trigger_stream.py, or any other device/agent),
it performs the WebRTC signaling handshake with the camera and
starts consuming video frames peer-to-peer.
"""

import argparse
import asyncio
import logging
import os
import sys
import time

try:
    import numpy as np
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
from device_connect_edge.drivers import DeviceDriver, rpc, emit, on
from device_connect_edge.types import DeviceIdentity, DeviceStatus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-24s  %(levelname)-7s  %(message)s",
)
logger = logging.getLogger("webrtc.display")


# ── WebRTC config ────────────────────────────────────────────────

RTC_CONFIG = RTCConfiguration(
    iceServers=[RTCIceServer(urls=["stun:stun.l.google.com:19302"])]
)


# ── Display driver ───────────────────────────────────────────────


class DisplayDriver(DeviceDriver):
    """Display device that receives WebRTC video from camera devices."""

    device_type = "display"

    def __init__(self, auto_camera: str | None = None):
        super().__init__()
        self._auto_camera = auto_camera
        self._peers: dict[str, RTCPeerConnection] = {}
        self._frame_counts: dict[str, int] = {}
        self._frame_start: dict[str, float] = {}

    @property
    def identity(self) -> DeviceIdentity:
        return DeviceIdentity(
            device_type="display",
            manufacturer="Artly",
            model="WebRTC-Display-1",
            firmware_version="0.1.0",
            description="Display that receives WebRTC video streams",
        )

    @property
    def status(self) -> DeviceStatus:
        return DeviceStatus(
            location="lab",
            availability="busy" if self._peers else "idle",
        )

    # ── Stream control RPCs ──────────────────────────────────────

    @rpc()
    async def watch_camera(self, camera_device_id: str) -> dict:
        """Start watching a camera's WebRTC video stream.

        Performs the full signaling handshake:
          1. Calls camera.start_stream → receives SDP offer
          2. Creates local PeerConnection, generates SDP answer
          3. Calls camera.accept_answer → handshake complete
          4. Video frames flow peer-to-peer via WebRTC

        Args:
            camera_device_id: The camera device to stream from.
        """
        if camera_device_id in self._peers:
            return {"error": f"already watching {camera_device_id}"}

        # ── Step 1: request an SDP offer from the camera ─────────
        logger.info("Requesting stream from %s ...", camera_device_id)
        resp = await self.invoke_remote(
            camera_device_id,
            "start_stream",
            timeout=15.0,
            caller_device_id=self._device_id,
        )
        offer_data = resp.get("result", resp)
        if "error" in offer_data:
            return {"error": f"camera refused: {offer_data['error']}"}

        # ── Step 2: create local PeerConnection, set remote offer ─
        pc = RTCPeerConnection(configuration=RTC_CONFIG)
        self._peers[camera_device_id] = pc
        self._frame_counts[camera_device_id] = 0
        self._frame_start[camera_device_id] = time.time()

        @pc.on("track")
        def _on_track(track: MediaStreamTrack):
            logger.info("Receiving %s track from %s", track.kind, camera_device_id)
            if track.kind == "video":
                asyncio.ensure_future(
                    self._consume_video(camera_device_id, track)
                )

        @pc.on("connectionstatechange")
        async def _on_state():
            state = pc.connectionState
            logger.info("[peer %s] state → %s", camera_device_id, state)
            if state in ("failed", "closed"):
                self._close_peer(camera_device_id)

        offer = RTCSessionDescription(
            sdp=offer_data["sdp"], type=offer_data["type"],
        )
        await pc.setRemoteDescription(offer)

        # ── Step 3: create answer, gather ICE, send to camera ────
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        # Wait for ICE gathering
        for _ in range(50):
            if pc.iceGatheringState == "complete":
                break
            await asyncio.sleep(0.1)

        resp = await self.invoke_remote(
            camera_device_id,
            "accept_answer",
            timeout=15.0,
            caller_device_id=self._device_id,
            sdp=pc.localDescription.sdp,
            type=pc.localDescription.type,
        )
        accept_data = resp.get("result", resp)
        if "error" in accept_data:
            self._close_peer(camera_device_id)
            return {"error": f"camera rejected answer: {accept_data['error']}"}

        logger.info("Stream established with %s", camera_device_id)
        return {"status": "streaming", "from": camera_device_id}

    @rpc()
    async def stop_watching(self, camera_device_id: str) -> dict:
        """Stop watching a camera stream.

        Notifies the camera to tear down its side, then cleans up locally.

        Args:
            camera_device_id: The camera to disconnect from.
        """
        if camera_device_id not in self._peers:
            return {"error": f"not watching {camera_device_id}"}

        # Tell the camera to release resources for our session
        try:
            await self.invoke_remote(
                camera_device_id,
                "stop_stream",
                timeout=5.0,
                caller_device_id=self._device_id,
            )
        except Exception as exc:
            logger.warning("Could not notify camera %s: %s", camera_device_id, exc)

        count = self._frame_counts.pop(camera_device_id, 0)
        elapsed = time.time() - self._frame_start.pop(camera_device_id, time.time())
        self._close_peer(camera_device_id)

        logger.info("Stopped watching %s (%d frames in %.1fs)",
                     camera_device_id, count, elapsed)
        return {"status": "stopped", "frames_received": count}

    @rpc()
    async def list_streams(self) -> dict:
        """List active incoming WebRTC streams."""
        return {
            "streams": [
                {
                    "camera": cid,
                    "state": pc.connectionState,
                    "frames": self._frame_counts.get(cid, 0),
                }
                for cid, pc in self._peers.items()
            ],
        }

    # ── React to camera stream-state events ──────────────────────

    @on(device_type="camera", event_name="stream_state_changed")
    async def on_stream_state(self, device_id: str, event_name: str, payload: dict):
        """Handle camera-side stream state changes.

        If the camera reports 'failed' or 'closed' for our session,
        clean up the local peer connection.
        """
        peer = payload.get("peer_device_id")
        state = payload.get("state")
        if peer == self._device_id and state in ("failed", "closed"):
            logger.warning("Camera %s stream %s — cleaning up", device_id, state)
            self._close_peer(device_id)

    # ── Frame processing ─────────────────────────────────────────

    async def _consume_video(self, camera_id: str, track: MediaStreamTrack):
        """Read and process video frames from a WebRTC track.

        Currently logs frame stats every second. Replace the comment
        block with your own processing (inference, display, save, etc.).
        """
        logger.info("Frame consumer started for %s", camera_id)
        while True:
            try:
                frame = await track.recv()
            except Exception:
                logger.info("Track from %s ended", camera_id)
                break

            self._frame_counts[camera_id] = self._frame_counts.get(camera_id, 0) + 1
            count = self._frame_counts[camera_id]

            # Log stats once per second (~every fps frames)
            if count % 30 == 0:
                elapsed = time.time() - self._frame_start.get(camera_id, time.time())
                fps = count / elapsed if elapsed > 0 else 0
                img = frame.to_ndarray(format="bgr24")
                logger.info(
                    "[%s] frame #%d  %dx%d  %.1f fps avg",
                    camera_id, count, img.shape[1], img.shape[0], fps,
                )

            # ── YOUR PROCESSING HERE ─────────────────────────────
            # img = frame.to_ndarray(format="bgr24")   # H×W×3 numpy array
            # detections = model.predict(img)
            # cv2.imshow("stream", img)

    # ── Internal ─────────────────────────────────────────────────

    def _close_peer(self, device_id: str):
        pc = self._peers.pop(device_id, None)
        if pc:
            asyncio.ensure_future(pc.close())
        self._frame_counts.pop(device_id, None)
        self._frame_start.pop(device_id, None)

    async def connect(self) -> None:
        logger.info("Display driver ready  (device_id=%s)", self._device_id)
        # Auto-connect to a camera if requested via --camera
        if self._auto_camera:
            asyncio.ensure_future(self._delayed_auto_connect())

    async def _delayed_auto_connect(self):
        """Wait for mesh discovery then connect to the camera."""
        logger.info("Will auto-connect to %s in 3s ...", self._auto_camera)
        await asyncio.sleep(3.0)
        try:
            result = await self.watch_camera(camera_device_id=self._auto_camera)
            logger.info("Auto-connect result: %s", result)
        except Exception as exc:
            logger.error("Auto-connect to %s failed: %s", self._auto_camera, exc)

    async def disconnect(self) -> None:
        for cid in list(self._peers):
            self._close_peer(cid)
        logger.info("Display driver disconnected")


# ── Entrypoint ───────────────────────────────────────────────────


async def run(
    device_id: str = "display-001",
    camera: str | None = None,
):
    """Run the display device standalone."""
    allow_insecure = os.getenv(
        "DEVICE_CONNECT_ALLOW_INSECURE", ""
    ).lower() in ("1", "true", "yes")

    device = DeviceRuntime(
        driver=DisplayDriver(auto_camera=camera),
        device_id=device_id,
        allow_insecure=allow_insecure,
    )
    await device.run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WebRTC Display Device")
    parser.add_argument("--device-id", default="display-001",
                        help="Device ID to register as (default: display-001)")
    parser.add_argument("--camera", default=None,
                        help="Auto-connect to this camera device on startup")
    args = parser.parse_args()

    try:
        asyncio.run(run(device_id=args.device_id, camera=args.camera))
    except KeyboardInterrupt:
        sys.exit(0)

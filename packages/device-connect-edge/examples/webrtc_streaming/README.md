# WebRTC Streaming Example

Stream video between two device-connect devices using WebRTC. The
device-connect mesh (NATS or Zenoh) carries only the signaling messages
(SDP offer/answer); the actual video frames flow peer-to-peer over
WebRTC's DTLS-SRTP transport.

## Architecture

```
┌──────────────┐        NATS (signaling only)        ┌──────────────┐
│              │  ──── start_stream (SDP offer) ────▸ │              │
│    Camera    │  ◂── accept_answer (SDP answer) ──── │   Display    │
│  (producer)  │                                      │  (consumer)  │
│              │  ═══════ WebRTC P2P video ══════════▸ │              │
└──────────────┘          (DTLS-SRTP)                 └──────────────┘
                                ▲
                                │  trigger_stream.py
                                │  (watch / stop / list)
                         ┌──────┴──────┐
                         │   Trigger   │
                         │    (CLI)    │
                         └─────────────┘
```

### Signaling flow

| Step | Mechanism | Direction |
|------|-----------|-----------|
| 1. Display calls `camera.start_stream` | `@rpc` via `invoke_remote` | display → camera |
| 2. Camera returns SDP offer | RPC response | camera → display |
| 3. Display creates answer, calls `camera.accept_answer` | `@rpc` via `invoke_remote` | display → camera |
| 4. Camera emits `stream_state_changed` | `@emit` / `@on` | camera → display |
| 5. Video frames flow over WebRTC | peer-to-peer (not NATS) | camera → display |

ICE candidates are gathered before the SDP is sent (no trickle ICE),
so the handshake completes in two RPCs.

## Files

| File | Description |
|------|-------------|
| `camera_webrtc.py` | Camera device — generates a test-pattern video track and serves WebRTC offers |
| `display_webrtc.py` | Display device — requests streams from cameras, consumes video frames |
| `trigger_stream.py` | CLI tool — start, stop, or list streams via one-shot RPCs |

## Prerequisites

```bash
pip install device-connect-edge aiortc numpy
```

`aiortc` requires system libraries for media codecs. On Ubuntu/Debian:

```bash
apt-get install libavdevice-dev libavfilter-dev libopus-dev libvpx-dev pkg-config
```

On macOS:

```bash
brew install ffmpeg opus libvpx pkg-config
```

## Running

### 1. Start the camera

```bash
export NATS_URL=nats://fabric.deviceconnect.dev:4222
export NATS_CREDENTIALS_FILE=path/to/camera-credentials.creds.json

python camera_webrtc.py --device-id camera-001 --fps 30
```

The camera registers on the mesh and waits for stream requests. It
generates a colour-cycling test pattern with a moving white bar — no
real camera hardware needed.

### 2. Start the display

In a second terminal:

```bash
export NATS_URL=nats://fabric.deviceconnect.dev:4222
export NATS_CREDENTIALS_FILE=path/to/display-credentials.creds.json

# Auto-connect to a camera on startup:
python display_webrtc.py --device-id display-001 --camera camera-001

# Or start idle and connect later via trigger:
python display_webrtc.py --device-id display-001
```

The display logs frame stats every 30 frames. Replace the processing
section in `_consume_video` with your own logic (inference, rendering,
saving to disk, etc.).

### 3. Control streams with the trigger (optional)

In a third terminal:

```bash
export NATS_URL=nats://fabric.deviceconnect.dev:4222
export NATS_CREDENTIALS_FILE=path/to/trigger-credentials.creds.json

# Start a stream
python trigger_stream.py watch display-001 camera-001

# Check active streams
python trigger_stream.py list camera-001
python trigger_stream.py list display-001

# Stop a stream
python trigger_stream.py stop display-001 camera-001
```

### D2D mode (no NATS server)

If you omit `NATS_URL` and `NATS_CREDENTIALS_FILE`, the devices fall
back to Zenoh D2D multicast. All three scripts work the same way — just
run them on the same network:

```bash
export DEVICE_CONNECT_ALLOW_INSECURE=true
python camera_webrtc.py &
python display_webrtc.py --camera camera-001 &
```

## Credential notes

- Each device needs credentials that authorize its `device_id` on the
  NATS server. If your JWT is scoped to a specific device, the
  `--device-id` flag must match.
- If using a shared credential (e.g. for development), you can run all
  three scripts with the same `NATS_CREDENTIALS_FILE` as long as the
  server allows multiple device registrations under that JWT.
- `DeviceRuntime` reads `NATS_URL` and `NATS_CREDENTIALS_FILE` from
  environment variables automatically — no code changes needed.

## Extending this example

- **Real camera**: Replace `TestVideoTrack` in `camera_webrtc.py` with
  `aiortc.contrib.media.MediaPlayer("/dev/video0", format="v4l2")`.
- **Audio**: Add an audio track to the peer connection alongside video.
- **Bidirectional**: Both sides can add tracks — useful for
  video-calling between two robots.
- **Data channel**: Add `pc.createDataChannel("control")` for a
  low-latency side channel (e.g. PTZ commands) alongside the video.
- **Frame processing**: In `display_webrtc.py`, the `_consume_video`
  method gives you each frame as an `av.VideoFrame`. Call
  `frame.to_ndarray(format="bgr24")` for a numpy array and run
  inference, OpenCV display, etc.

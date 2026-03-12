<p align="center">
  <img src="../../logo.svg" alt="Device Connect" width="400">
</p>

# device-connect-sdk

Lightweight Python SDK for enabling physical devices to work with Device Connect. You write the device logic; the runtime handles registration, heartbeats, and command routing.

## Contents

- [Where This Fits](#where-this-fits)
- [Install](#install)
- [Decorators](#decorators)
- [Quick Start](#quick-start)
- [Device-to-Device Mode](#device-to-device-mode-no-infrastructure)
- [Credentials](#credentials)
- [Testing](#testing)
- [Contributing](#contributing)

## Where This Fits

```
  device-connect-sdk        device-connect-server         device-connect-agent-tools
  (Device Connect SDK — this) (server runtime)    (agent SDK)
        │                       │                       │
        └──────────── Device Connect Mesh ──────────────────────┘
```

- **device-connect-sdk** — runs on physical devices (Raspberry Pi, robots, cameras, sensors)
- **device-connect-server** — runs on servers. Adds registry, security, state, and CLIs
- **device-connect-agent-tools** — connects AI agents (Strands, LangChain, MCP) to the device mesh

## Install

> Not yet on PyPI. Install from Git:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install "device-connect-sdk @ git+https://github.com/arm/device-connect.git#subdirectory=packages/device-connect-sdk"
```

## Decorators

| Decorator | Purpose |
|-----------|---------|
| `@rpc()` | Expose a method as a remotely-callable function |
| `@emit()` | Declare an event that can be published to subscribers |
| `@periodic(interval=N)` | Run a method every N seconds in the background |
| `@on(device_type=..., event_name=...)` | Subscribe to events from other devices (D2D) |
| `@before_emit("event_name")` | Intercept an event before it's published |

## Quick Start

After installing the Device Connect SDK, write a driver and run it.

### 1. Write a driver

```python
from device_connect_sdk.drivers import DeviceDriver, rpc, emit, periodic
from device_connect_sdk.types import DeviceIdentity, DeviceStatus

class SensorDriver(DeviceDriver):
    device_type = "sensor"

    @property
    def identity(self) -> DeviceIdentity:
        return DeviceIdentity(device_type="sensor", manufacturer="Acme", model="TH-100")

    @property
    def status(self) -> DeviceStatus:
        return DeviceStatus(availability="available")

    @rpc()
    async def get_reading(self) -> dict:
        """Return the current sensor reading."""
        return {"temperature": 22.5, "humidity": 45}

    @emit()
    async def alert(self, level: str, message: str):
        """Emit an alert event."""
        pass

    @periodic(interval=10.0)
    async def poll_sensor(self):
        reading = await self.get_reading()
        if reading["temperature"] > 30:
            await self.alert(level="warning", message="High temperature")

    async def connect(self) -> None:
        pass  # initialize hardware

    async def disconnect(self) -> None:
        pass  # cleanup hardware
```

### 2. Connect to the mesh

```python
import asyncio
from device_connect_sdk import DeviceRuntime

async def main():
    device = DeviceRuntime(
        driver=SensorDriver(),
        device_id="sensor-001",
        messaging_urls=["tcp/localhost:7447"],
        # Or use NATS:
        # messaging_urls=["nats://localhost:4222"],
    )
    await device.run()

asyncio.run(main())
```

### 3. Run the simulator

Save the code above to `my_sensor.py` and run it:

```bash
# Zenoh (default) — or omit messaging_urls entirely for P2P mode
DEVICE_CONNECT_ALLOW_INSECURE=true python my_sensor.py

# Or NATS
# DEVICE_CONNECT_ALLOW_INSECURE=true NATS_URL=nats://localhost:4222 python my_sensor.py
```

### 4. More examples

| Example | Description |
|---------|-------------|
| [`examples/number_generator/`](examples/number_generator/) | Simulated random number generator with on-demand and periodic emission |
| [`examples/string_generator/`](examples/string_generator/) | Simulated random word fragment generator with mood themes |
| [`examples/dht22_sensor/`](examples/dht22_sensor/) | Real DHT22 temperature/humidity sensor on Raspberry Pi |

> **Real hardware drivers** run as a Python process on the physical device and require credentials provisioned by [device-connect-server](../device-connect-server/).

```bash
# Real hardware (on the device)
NATS_CREDENTIALS_FILE=~/.device-connect/credentials/dht22-001.creds.json python examples/dht22_sensor/device_driver.py
```

## Device-to-Device Mode (No Infrastructure)

Devices can discover each other directly on the LAN without any infrastructure (no broker, no etcd, no device registry). This uses Zenoh's built-in multicast scouting.

**P2P mode is the default** when no broker endpoint URLs are configured:

```python
device = DeviceRuntime(
    driver=SensorDriver(),
    device_id="sensor-001",
    allow_insecure=True,
    # No messaging_urls → Zenoh peer mode with multicast discovery
)
await device.run()
```

Or via environment variables:

```bash
DEVICE_CONNECT_ALLOW_INSECURE=true python my_device.py
```

To force D2D mode even when a router URL is set (e.g., router available but no registry):

```bash
DEVICE_CONNECT_DISCOVERY_MODE=d2d ZENOH_CONNECT=tcp/localhost:7447 DEVICE_CONNECT_ALLOW_INSECURE=true python my_device.py
```

**How it works:** Each device announces its presence (capabilities, identity, status) via `device-connect.{tenant}.{device_id}.presence` messages. Other devices subscribe to a wildcard and maintain an in-memory peer table. Device-to-device RPC works identically to infrastructure mode.

**Trade-offs vs full infrastructure:**

| | Full Infrastructure | D2D Mode |
|---|---|---|
| Device state | Persistent (etcd) | Ephemeral (in-memory) |
| Offline tracking | Registry remembers devices | Gone when device stops |
| Cross-network | Zenoh router bridges LANs | LAN only (multicast) |
| Scale | 1000s of devices | ~50-100 devices |

## Credentials

Credentials are generated server-side using device-connect-server's provisioning tools. See [device-connect-server — Device Commissioning](../device-connect-server/README.md#device-commissioning-flow).

The credentials file is JSON with JWT and NKey seed:

```json
{
  "device_id": "sensor-001",
  "auth_type": "jwt",
  "tenant": "default",
  "nats": {
    "urls": ["nats://nats-jwt:4222"],
    "jwt": "<NATS user JWT>",
    "nkey_seed": "<NKey seed>"
  }
}
```

Pass the file path via environment variable or constructor parameter:

```bash
# Via environment variable
NATS_CREDENTIALS_FILE=~/.device-connect/credentials/sensor-001.creds.json \
  NATS_URL=nats://localhost:4222 python my_device.py
```

```python
# Via constructor
device = DeviceRuntime(
    driver=SensorDriver(),
    device_id="sensor-001",
    nats_credentials_file="~/.device-connect/credentials/sensor-001.creds.json",
    messaging_urls=["nats://localhost:4222"],
)
```

For development without auth, set `DEVICE_CONNECT_ALLOW_INSECURE=true` or pass `allow_insecure=True` to `DeviceRuntime`.

## Testing

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/ -v --timeout=30
```

Unit tests run without external services. Integration tests are in [tests/](../../tests/).

## Contributing

We welcome contributions! Please open an [issue](https://github.com/arm/device-connect/issues) to report bugs or suggest features, or submit a [pull request](https://github.com/arm/device-connect/pulls) directly.

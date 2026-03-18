# Device Connect

An open-source Python framework by Arm for connecting IoT devices, robots, and AI agents. Works device-to-device with zero infrastructure, or scales to thousands of devices with a pub/sub router. Supports Zenoh, NATS, and MQTT backends.

## Packages

This is a monorepo containing three packages and a cross-package integration test suite.

| Package | Description | Install |
|---------|-------------|---------|
| [`device-connect-sdk`](packages/device-connect-sdk/) | Edge SDK — `DeviceDriver`, `DeviceRuntime`, `@rpc`/`@emit` decorators | `pip install device-connect-sdk` |
| [`device-connect-server`](packages/device-connect-server/) | Server — registry, security, state management, `devctl`/`statectl` CLIs | `pip install device-connect-server` |
| [`device-connect-agent-tools`](packages/device-connect-agent-tools/) | Agent SDK — `discover_devices`, `invoke_device`, Strands/LangChain/MCP adapters | `pip install device-connect-agent-tools` |
| [`tests/`](tests/) | Integration tests — D2D, D2O, messaging conformance, LLM orchestration | — |

## Architecture

```
┌──────────────────┐     ┌──────────────────┐     ┌─────────────────────┐
│  Devices         │     │                  │     │  AI Agents          │
│  (SDK)           │     │                  │     │  (agent-tools)      │
│  DeviceDriver    │◄───►│     Pub Sub      │◄───►│  discover_devices() │
│  @rpc  @emit     │     │                  │     │  invoke_device()    │
│  @periodic  @on  │     │                  │     │  Strands / LC / MCP │
└──────────────────┘     └────────┬─────────┘     └─────────────────────┘
                                  │
                         ┌────────┴─────────┐
                         │  Server (opt.)   │
                         │  Registry, KV    │
                         │  Security, CLIs  │
                         └──────────────────┘
```

## Quick Start: D2D Mode (Zero Infrastructure)

The fastest way to get started — no Docker, no broker, no configuration. Zenoh discovers peers on the local network via multicast scouting.

### 1. Install the SDK

```bash
pip install device-connect-sdk
```

### 2. Write a device driver

```python
# my_sensor.py
import asyncio
from device_connect_sdk import DeviceRuntime
from device_connect_sdk.drivers import DeviceDriver, rpc, emit

class SensorDriver(DeviceDriver):
    device_type = "sensor"

    @rpc()
    async def get_reading(self) -> dict:
        """Return the current sensor reading."""
        return {"temperature": 22.5, "humidity": 45}

    @emit()
    async def alert(self, level: str, message: str):
        pass

async def main():
    device = DeviceRuntime(
        driver=SensorDriver(),
        device_id="sensor-001",
    )
    await device.run()

asyncio.run(main())
```

### 3. Run it

```bash
DEVICE_CONNECT_ALLOW_INSECURE=true python my_sensor.py
```

The device starts in D2D mode automatically — no URLs needed. Any other Device Connect device on the same network discovers it instantly.

> **Note:** `DEVICE_CONNECT_ALLOW_INSECURE=true` skips TLS verification and is intended for local development only. Production deployments should use proper certificates.

### 4. Discover and invoke from an agent

In a second terminal:

```bash
pip install device-connect-agent-tools
```

```python
import asyncio
from device_connect_agent_tools import connect, discover_devices, invoke_device

async def main():
    await connect()
    devices = await discover_devices(device_type="sensor")
    print(devices)

    result = await invoke_device("sensor-001", "get_reading")
    print(result)  # {"temperature": 22.5, "humidity": 45}

asyncio.run(main())
```

## Quick Start: With Infrastructure

For production deployments with a Zenoh router, device registry, and distributed state. Infrastructure gives you distributed state and locks, cross-network routing, and a device registry with commissioning and lease management.

```bash
# Start Zenoh router + etcd + registry
cd packages/device-connect-server
docker compose -f infra/docker-compose-dev.yml up -d

# Connect your device to the router
DEVICE_CONNECT_ALLOW_INSECURE=true \
ZENOH_CONNECT=tcp/localhost:7447 \
python my_sensor.py

# Verify with devctl
pip install device-connect-server
devctl list
```

## Companion: Strands Robots

[**Strands Robots**](https://github.com/cagataycali/strands-gtc-nvidia) provides production-ready Device Connect drivers for robotics:

- **`RobotDeviceDriver`** — wraps any Strands robot (SO-100, Koch, etc.) as a Device Connect device with `execute`, `stop`, `getStatus` RPCs and `taskStarted`/`taskComplete` events
- **`SimulationDeviceDriver`** — wraps MuJoCo/Newton/Isaac Sim simulations as devices
- **`ReachyMiniDriver`** — specialized driver for Pollen Reachy Mini robots

```python
from strands_robots import Robot

robot = Robot("so100")
robot.run()  # discoverable and invocable by AI agents and devices on the network
```

See [`strands_robots/device_connect/`](https://github.com/cagataycali/strands-gtc-nvidia/tree/main/strands_robots/device_connect) for the full integration.

## Development

### Editable install (all packages)

```bash
pip install -e packages/device-connect-sdk
pip install -e "packages/device-connect-server[all]"
pip install -e "packages/device-connect-agent-tools[strands]"
```

### Running tests

```bash
# SDK unit tests (no Docker)
cd packages/device-connect-sdk && python3 -m pytest tests/ -v

# Server unit tests (no Docker)
cd packages/device-connect-server && python3 -m pytest tests/ -v

# Agent-tools unit tests (no Docker)
cd packages/device-connect-agent-tools && python3 -m pytest tests/test_connection_unit.py tests/test_tools_unit.py -v

# Integration tests (requires Docker)
cd tests && docker compose -f docker-compose-itest.yml up -d
DEVICE_CONNECT_ALLOW_INSECURE=true python3 -m pytest tests/ -v -m "not llm"
```

See [tests/README.md](tests/README.md) for the full test matrix.

## License

Apache License 2.0 — see [LICENSE](LICENSE).

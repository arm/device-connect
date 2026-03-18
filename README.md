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
curl -LsSf https://astral.sh/uv/install.sh | sh  # skip if uv already installed
uv venv && source .venv/bin/activate && uv pip install device-connect-sdk
```

### 2. Write a device driver

```python
# sensor.py
import asyncio
from device_connect_sdk import DeviceRuntime
from device_connect_sdk.drivers import DeviceDriver, rpc

class SensorDriver(DeviceDriver):
    device_type = "sensor"

    @rpc()
    async def get_reading(self) -> dict:
        """Return the current sensor reading."""
        return {"temperature": 22.5, "humidity": 45}

asyncio.run(DeviceRuntime(driver=SensorDriver(), device_id="sensor-001").run())
```

### 3. Run it

```bash
TERM=xterm screen -S sensor
source .venv/bin/activate
DEVICE_CONNECT_ALLOW_INSECURE=true python sensor.py
# Ctrl+a d to detach, screen -r sensor to reattach
```

The device starts in D2D mode automatically — no URLs needed. Any other Device Connect device on the same network discovers it instantly.

> **Note:** `DEVICE_CONNECT_ALLOW_INSECURE=true` skips TLS and is intended for local development only.

### 4. Discover and invoke from an agent

```bash
source .venv/bin/activate
uv pip install device-connect-agent-tools
```

```bash
DEVICE_CONNECT_ALLOW_INSECURE=true python -c "
from device_connect_agent_tools import connect, discover_devices, invoke_device
connect()
devices = discover_devices(device_type='sensor')
print(devices)
result = invoke_device('sensor-001', 'get_reading')
print(result)  # {'temperature': 22.5, 'humidity': 45}
"
```

## Quick Start: With Infrastructure

For quick start with a router, device registry, and distributed state — see [`packages/device-connect-server`](packages/device-connect-server/README.md).

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

## Security

Device Connect supports encryption in transit (TLS/mTLS), JWT/NKey authentication for NATS, device commissioning with PIN validation, and per-device ACLs. This is an area of active development to be further expanded in upcoming releases.

## License

Apache License 2.0 — see [LICENSE](LICENSE).

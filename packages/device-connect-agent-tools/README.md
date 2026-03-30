# device-connect-agent-tools

Framework-agnostic tools for Device Connect — discover and invoke devices from any AI agent. Plain Python functions at the core, with adapters for [Strands](#strands-agent), [LangChain](#langchain--langgraph), and [MCP](#mcp-bridge).

## Contents

- [Where This Fits](#where-this-fits)
- [Install](#install)
- [Examples](#examples)
- [Architecture](#architecture)
- [Quick Start](#quick-start)
  - [Plain Python](#plain-python-no-framework)
  - [Strands Agent](#strands-agent)
  - [LangChain / LangGraph](#langchain--langgraph)
  - [MCP Bridge](#mcp-bridge)
- [Connection](#connection)
  - [Auto-Discovery](#auto-discovery)
  - [JWT Credentials](#jwt-credentials)
  - [Explicit Configuration](#explicit-configuration)
  - [Environment Variables](#environment-variables)
  - [Device-to-Device Mode](#device-to-device-mode-no-infrastructure)
- [Tools](#tools)
- [Event Subscription](#event-subscription)
- [DeviceConnectMCP — Build Devices with Decorators](#deviceconnectmcp--build-devices-with-decorators)
- [Writing an Adapter](#writing-an-adapter)
- [API Reference](#api-reference)
- [Contributing](#contributing)

## Where This Fits

```
  device-connect-sdk          device-connect-server           device-connect-agent-tools
  (Device Connect SDK)    (server runtime)          (agent SDK — this)
        │                         │                         │
        └──────────── Device Connect Mesh ─────────────────────────┘
```

- **[device-connect-sdk](../device-connect-sdk/)** — runs on physical devices (Raspberry Pi, robots, cameras, sensors)
- **[device-connect-server](../device-connect-server/)** — runs on servers. Adds registry, security, state, and CLIs
- **device-connect-agent-tools** — connects AI agents (Strands, LangChain, MCP) to the device mesh

## Install

> Not yet on PyPI. Install from Git:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install "device-connect-agent-tools @ git+https://github.com/arm/device-connect.git#subdirectory=packages/device-connect-agent-tools"
```

Optional extras:

| Extra | Adds |
|-------|------|
| `[strands]` | [Strands Agents](https://strandsagents.com) adapter |
| `[langchain]` | [LangChain](https://python.langchain.com) adapter |
| `[mcp]` | [FastMCP](https://github.com/jlowin/fastmcp) for MCP bridge |
| `[dev]` | pytest + dev tools |

```bash
# Example: install with Strands adapter
pip install -e ".[strands]"
```

## Examples

- **[strands-device-connect-example](https://github.com/arm/strands-device-connect-example)** — Event-driven Strands Agent that monitors device events and reacts

## Architecture

```
                    ┌──────────────────────────┐
                    │      Your AI Agent       │
                    │  (Strands / LangChain /  │
                    │         MCP)             │
                    └────────┬─────────────────┘
                             │  imports adapter
                    ┌────────▼─────────────────────────┐
                    │  device-connect-agent-tools      │
                    │                                  │
                    │  discover_devices()              │
                    │  invoke_device()                 │  JSON-RPC over
                    │  get_device_status()             │  Zenoh / NATS
                    │  invoke_device_with_fallback()   │──────────┐
                    │                                  │          │
                    │  connect() / disconnect()        │          │
                    └──────────────────────────────────┘          │
                                                        ┌────────▼────────┐
                                                        │  Device Connect Mesh    │
                                                        │  ┌────────────┐ │
                                                        │  │ Camera     │ │
                                                        │  │ Robot      │ │
                                                        │  │ Sensor     │ │
                                                        │  └────────────┘ │
                                                        └─────────────────┘
```

## Quick Start

### Plain Python (no framework)

```python
from device_connect_agent_tools import connect, disconnect, discover_devices, invoke_device

connect()

devices = discover_devices()
for d in devices:
    print(f"{d['device_id']} ({d['device_type']}): {[f['name'] for f in d['functions']]}")

result = invoke_device("robot-001", "get_status")
print(result)

disconnect()
```

### Strands Agent

```python
from strands import Agent
from strands.models import AnthropicModel
from device_connect_agent_tools import connect
from device_connect_agent_tools.adapters.strands import discover_devices, invoke_device

connect()

agent = Agent(
    model=AnthropicModel(model_id="claude-sonnet-4-20250514"),
    tools=[discover_devices, invoke_device],
    system_prompt="You manage devices on a Device Connect network.",
)

agent("What devices are online? Get the status of each one.")
```

### LangChain / LangGraph

```python
from langchain_anthropic import ChatAnthropic
from langgraph.prebuilt import create_react_agent
from device_connect_agent_tools import connect
from device_connect_agent_tools.adapters.langchain import discover_devices, invoke_device

connect()

model = ChatAnthropic(model="claude-sonnet-4-20250514")
agent = create_react_agent(model, tools=[discover_devices, invoke_device])

result = agent.invoke({"messages": [{"role": "user", "content": "What devices are online?"}]})
print(result["messages"][-1].content)
```

### MCP Bridge

The MCP bridge discovers devices on the Device Connect mesh and exposes them as MCP tools.

Add to your MCP client config (e.g., `~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
    "mcpServers": {
        "device-connect": {
            "command": "python",
            "args": ["-m", "device_connect_agent_tools.mcp"],
            "env": {
                "ZENOH_CONNECT": "tcp/localhost:7447",
                "DEVICE_CONNECT_ALLOW_INSECURE": "true"
            }
        }
    }
}
```

Each device function appears as a tool in the MCP client. The agent can discover devices, invoke functions, and read status — all routed through the Device Connect mesh.

## Connection

### Auto-Discovery

`connect()` with no arguments works when run from anywhere inside a Device Connect project tree:

```python
from device_connect_agent_tools import connect

connect()  # auto-discovers broker URL, credentials, and TLS certs
```

It searches upward from the current directory for `security_infra/credentials/` and `security_infra/certs/`.

### JWT Credentials

Agents need their own credentials generated by [device-connect-server](../device-connect-server/README.md#infrastructure):

```bash
# On the server: generate agent credentials
./security_infra/gen_creds.sh --user my-agent
```

Then connect using the credentials file:

```bash
NATS_CREDENTIALS_FILE=~/.device-connect/credentials/my-agent.creds.json \
  NATS_URL=nats://localhost:4222 python my_agent.py
```

Or pass credentials explicitly:

```python
connect(
    messaging_urls=["nats://localhost:4222"],
    credentials={"jwt": "...", "nkey_seed": "..."},
)
```

### Explicit Configuration

```python
connect(
    messaging_urls=["tls://nats.example.com:4222"],
    zone="production",
    credentials={"jwt": "...", "nkey_seed": "..."},
    tls_config={"ca_file": "/path/to/ca.pem"},
)
```

### Environment Variables

| Variable | Description |
|---|---|
| `ZENOH_CONNECT` | Zenoh endpoint (e.g., `tcp/localhost:7447`) |
| `MESSAGING_BACKEND` | `zenoh` (default), `nats`, or `mqtt` |
| `MESSAGING_URLS` | Broker URLs, comma-separated (generic) |
| `NATS_URL` | NATS broker URL (when using NATS backend) |
| `NATS_CREDENTIALS_FILE` | Path to `.creds.json` file |
| `NATS_JWT` + `NATS_NKEY_SEED` | Direct JWT auth |
| `NATS_TLS_CA_FILE` | CA certificate for TLS |
| `TENANT` | Device Connect zone/namespace (default: `"default"`) |
| `DEVICE_CONNECT_DISCOVERY_MODE` | Set to `d2d` to skip registry and discover via presence |

Resolution order: explicit parameter > environment variable > auto-discovery.

### Device-to-Device Mode (No Infrastructure)

With no endpoint URLs configured, `discover_devices()` automatically uses D2D presence-based discovery (Zenoh multicast scouting) instead of querying the registry service. No Docker infrastructure needed:

```bash
export DEVICE_CONNECT_ALLOW_INSECURE=true
# No ZENOH_CONNECT → Zenoh multicast scouting on LAN (default)
python my_agent.py
```

Devices on the same LAN that are also running in D2D mode will be discovered automatically. See [device-connect-sdk](../device-connect-sdk/README.md#device-to-device-mode-no-infrastructure) for details.

## Tools

All four tools are plain Python functions. Import them directly for framework-free use, or from an adapter for framework integration:

```python
# Plain Python
from device_connect_agent_tools import discover_devices, invoke_device

# Strands (returns DecoratedFunctionTool)
from device_connect_agent_tools.adapters.strands import discover_devices, invoke_device

# LangChain (returns StructuredTool)
from device_connect_agent_tools.adapters.langchain import discover_devices, invoke_device
```

## Event Subscription

For event-driven agents that react to device events in real-time:

```python
import asyncio
from device_connect_agent_tools import connect, get_connection

connect()
conn = get_connection()

async def on_event(msg):
    print(f"Event on {msg.subject}: {msg.data.decode()}")

# Subscribe to all device events
sub = await conn.async_subscribe("device-connect.default.*.event.>", on_event)
```

Subject patterns:
- `device-connect.{zone}.*.event.>` — all events from all devices
- `device-connect.{zone}.{device_id}.event.>` — events from one device
- `device-connect.{zone}.{device_id}.event.words_generated` — specific event type

## DeviceConnectMCP — Build Devices with Decorators

`DeviceConnectMCP` provides a FastMCP-compatible API for building Device Connect devices with `@tool` and `@event` decorators. Auto-generates function schemas from type hints and docstrings.

```python
from device_connect_agent_tools.mcp import DeviceConnectMCP

mcp = DeviceConnectMCP(
    "cleaning-robot-001",
    device_type="cleaning_robot",
    manufacturer="Acme",
    location="warehouse-A",
)

@mcp.tool()
async def start_cleaning(zone: str = "all") -> dict:
    """Start cleaning in the specified zone."""
    return {"status": "started", "zone": zone}

@mcp.event()
async def cleaning_complete(zone: str, duration_seconds: int):
    """Emitted when cleaning is complete."""
    pass

await mcp.run()
```

## Writing an Adapter

To add support for another framework, wrap the plain functions:

```python
# device_connect_agent_tools/adapters/my_framework.py

from my_framework import wrap_tool
from device_connect_agent_tools.tools import (
    discover_devices as _discover_devices,
    invoke_device as _invoke_device,
    invoke_device_with_fallback as _invoke_device_with_fallback,
    get_device_status as _get_device_status,
)

discover_devices = wrap_tool(_discover_devices)
invoke_device = wrap_tool(_invoke_device)
invoke_device_with_fallback = wrap_tool(_invoke_device_with_fallback)
get_device_status = wrap_tool(_get_device_status)
```

## API Reference

### Connection

| Function | Description |
|---|---|
| `connect(messaging_urls, zone, credentials, tls_config)` | Initialize messaging connection |
| `disconnect()` | Close connection and release resources |
| `get_connection()` | Get current connection (auto-connects if needed) |

### Tools

| Function | Description |
|---|---|
| `discover_devices(device_type, refresh)` | List devices with function schemas |
| `invoke_device(device_id, function, params, llm_reasoning)` | Call a function on a device |
| `invoke_device_with_fallback(device_ids, function, params, llm_reasoning)` | Try multiple devices in order |
| `get_device_status(device_id)` | Get detailed device status |

### Connection Object

For advanced use via `get_connection()`:

| Method | Description |
|---|---|
| `conn.list_devices(device_type)` | List devices from registry |
| `conn.get_device(device_id)` | Get single device by ID |
| `conn.invoke(device_id, function, params)` | Direct JSON-RPC call |
| `conn.async_subscribe(subject, callback)` | Subscribe to messaging subject |
| `conn.messaging_client` | Underlying `MessagingClient` instance |

## Contributing

We welcome contributions! Please open an [issue](https://github.com/arm/device-connect/issues) to report bugs or suggest features, or submit a [pull request](https://github.com/arm/device-connect/pulls) directly.

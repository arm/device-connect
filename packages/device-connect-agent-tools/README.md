# device-connect-agent-tools

Framework-agnostic tools for Device Connect — discover and invoke devices from any AI agent. Plain Python functions at the core, with adapters for [Strands](#strands-agent), [LangChain](#langchain--langgraph), and [MCP](#mcp-bridge).

## Contents

- [device-connect-agent-tools](#device-connect-agent-tools)
  - [Contents](#contents)
  - [Where This Fits](#where-this-fits)
  - [Install](#install)
  - [Examples](#examples)
  - [Architecture](#architecture)
    - [Hierarchical Discovery](#hierarchical-discovery)
  - [Quick Start](#quick-start)
    - [Plain Python (no framework)](#plain-python-no-framework)
    - [Strands Agent](#strands-agent)
    - [LangChain / LangGraph](#langchain--langgraph)
    - [MCP Bridge](#mcp-bridge)
  - [Connection](#connection)
    - [Auto-Discovery](#auto-discovery)
    - [JWT Credentials](#jwt-credentials)
    - [Explicit Configuration](#explicit-configuration)
    - [Environment Variables](#environment-variables)
    - [Device-to-Device Mode (No Infrastructure)](#device-to-device-mode-no-infrastructure)
  - [Tools](#tools)
  - [Event Subscription](#event-subscription)
  - [DeviceConnectMCP — Build Devices with Decorators](#deviceconnectmcp--build-devices-with-decorators)
  - [Writing an Adapter](#writing-an-adapter)
  - [API Reference](#api-reference)
    - [Connection](#connection-1)
    - [Tools](#tools-1)
    - [Connection Object](#connection-object)
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

- **`StrandsDeviceConnectAgent`** (`device_connect_agent_tools.adapters.strands_agent`) — Event-driven Strands Agent that monitors device events and reacts

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
                    │  1. describe_fleet()             │
                    │  2. list_devices(type, location) │  JSON-RPC over
                    │  3. get_device_functions(id)     │  Zenoh / NATS
                    │  4. invoke_device(id, fn, args)  │──────────┐
                    │                                  │          │
                    │  connect() / disconnect()        │          │
                    └──────────────────────────────────┘          │
                                                        ┌─────────▼─────────────┐
                                                        │  Device Connect Mesh  │
                                                        │  ┌─────────────┐      │
                                                        │  │ Camera      │      │
                                                        │  │ Robot       │      │
                                                        │  │ Sensor      │      │
                                                        │  └─────────────┘      │
                                                        └───────────────────────┘
```

### Hierarchical Discovery

Protocols like MCP typically register every device function as a separate tool. With 50 devices averaging 10 functions each, the LLM sees 500 tool definitions — thousands of tokens of JSON Schema that crowd out space for actual reasoning.

This package exposes **4 meta-tools** that let the agent drill down progressively, loading detail only when needed:

```
describe_fleet()                       ~200 tokens  (counts by type and location)
  └─▸ list_devices(device_type=...)    ~50 tokens/device  (names only, no schemas)
        └─▸ get_device_functions(id)   full schemas for ONE device
              └─▸ invoke_device(...)   call a function
```

**Small-fleet shortcut:** When the fleet has 5 or fewer devices, `describe_fleet()` and `list_devices()` automatically include full function schemas in the response — the agent can skip straight to `invoke_device()` in one or two calls. The threshold is configurable via the `DEVICE_CONNECT_SMALL_FLEET_THRESHOLD` environment variable (set to `0` to always require drill-down).

**Example — an agent resolving "check the lobby cameras":**

**Step 1** — `describe_fleet()` returns a bird's-eye view (~200 tokens):

```json
{
  "total_devices": 47,
  "total_functions": 183,
  "by_type": {
    "camera":  {"count": 12, "locations": ["lobby", "warehouse", "parking"]},
    "robot":   {"count": 8,  "locations": ["warehouse"]},
    "sensor":  {"count": 27, "locations": ["lobby", "warehouse", "parking", "office"]}
  },
  "by_location": {
    "lobby":     {"count": 9,  "types": ["camera", "sensor"]},
    "warehouse": {"count": 31, "types": ["camera", "robot", "sensor"]}
  }
}
```

**Step 2** — Agent sees 12 cameras, narrows to lobby: `list_devices(device_type="camera", location="lobby")` — compact roster, no schemas:

```json
{
  "devices": [
    {"device_id": "cam-001", "device_type": "camera", "location": "lobby", "function_count": 3, "function_names": ["capture_image", "pan_tilt", "get_status"]},
    {"device_id": "cam-002", "device_type": "camera", "location": "lobby", "function_count": 3, "function_names": ["capture_image", "pan_tilt", "get_status"]}
  ],
  "total": 2, "offset": 0, "limit": 20, "has_more": false
}
```

**Step 3** — Agent picks cam-001, loads its schemas: `get_device_functions("cam-001")` — full detail for ONE device:

```json
{
  "device_id": "cam-001", "device_type": "camera", "location": "lobby",
  "functions": [{
    "name": "capture_image",
    "description": "Capture a still image",
    "parameters": {"type": "object", "properties": {"resolution": {"type": "string", "enum": ["720p", "1080p", "4k"]}}, "required": ["resolution"]}
  }]
}
```

**Step 4** — `invoke_device("cam-001", "capture_image", {"resolution": "1080p"})`

Total context: ~450 tokens across 4 calls, vs ~5 000+ if every function schema were loaded upfront. Each step gives the LLM a **focused decision** ("which type?" → "which device?" → "which function?" → "what params?") instead of a 500-way tool selection.

The same 4 tools are available in every adapter (Strands, LangChain, MCP) and in plain Python. The MCP bridge registers exactly these 4 tools — not one per device function — so MCP clients like Claude Desktop stay responsive regardless of fleet size.

## Quick Start

### Plain Python (no framework)

```python
from device_connect_agent_tools import (
    connect, disconnect, describe_fleet, list_devices, get_device_functions, invoke_device,
)

connect()

# 1. What's on the network?
fleet = describe_fleet()
print(fleet)  # {total_devices: 47, by_type: {camera: {count: 12}, ...}}

# 2. Browse a specific type
cameras = list_devices(device_type="camera", location="lobby")
for d in cameras["devices"]:
    print(f"{d['device_id']}: {d['function_names']}")

# 3. Get full schemas for one device
info = get_device_functions("cam-001")

# 4. Call a function
result = invoke_device("cam-001", "capture_image", {"resolution": "1080p"})
print(result)

disconnect()
```

### Strands Agent

```python
from strands import Agent
from strands.models import AnthropicModel
from device_connect_agent_tools import connect
from device_connect_agent_tools.adapters.strands import (
    describe_fleet, list_devices, get_device_functions, invoke_device,
)

connect()

agent = Agent(
    model=AnthropicModel(model_id="claude-sonnet-4-20250514"),
    tools=[describe_fleet, list_devices, get_device_functions, invoke_device],
    system_prompt="You manage devices on a Device Connect network.",
)

agent("Find all cameras in the lobby and capture an image from each one.")
```

### LangChain / LangGraph

```python
from langchain_anthropic import ChatAnthropic
from langgraph.prebuilt import create_react_agent
from device_connect_agent_tools import connect
from device_connect_agent_tools.adapters.langchain import (
    describe_fleet, list_devices, get_device_functions, invoke_device,
)

connect()

model = ChatAnthropic(model="claude-sonnet-4-20250514")
agent = create_react_agent(model, tools=[describe_fleet, list_devices, get_device_functions, invoke_device])

result = agent.invoke({"messages": [{"role": "user", "content": "What devices are online?"}]})
print(result["messages"][-1].content)
```

### MCP Bridge

The MCP bridge exposes the same 4 hierarchical meta-tools over the MCP protocol. Unlike traditional MCP servers that register one tool per device function, the bridge stays constant regardless of fleet size.

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

The MCP client sees `describe_fleet`, `list_devices`, `get_device_functions`, and `invoke_device` — the agent navigates the fleet the same way it would via Strands or LangChain.

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

With no endpoint URLs configured, the discovery tools automatically use D2D presence-based discovery (Zenoh multicast scouting) instead of querying the registry service. No Docker infrastructure needed:

```bash
export DEVICE_CONNECT_ALLOW_INSECURE=true
# No ZENOH_CONNECT → Zenoh multicast scouting on LAN (default)
python my_agent.py
```

Devices on the same LAN that are also running in D2D mode will be discovered automatically. See [device-connect-sdk](../device-connect-sdk/README.md#device-to-device-mode-no-infrastructure) for details.

## Tools

The 4 hierarchical discovery tools are plain Python functions. Import them directly or from an adapter:

```python
# Plain Python
from device_connect_agent_tools import describe_fleet, list_devices, get_device_functions, invoke_device

# Strands (returns DecoratedFunctionTool)
from device_connect_agent_tools.adapters.strands import describe_fleet, list_devices, get_device_functions, invoke_device

# LangChain (returns StructuredTool)
from device_connect_agent_tools.adapters.langchain import describe_fleet, list_devices, get_device_functions, invoke_device
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
    describe_fleet as _describe_fleet,
    list_devices as _list_devices,
    get_device_functions as _get_device_functions,
    invoke_device as _invoke_device,
)

describe_fleet = wrap_tool(_describe_fleet)
list_devices = wrap_tool(_list_devices)
get_device_functions = wrap_tool(_get_device_functions)
invoke_device = wrap_tool(_invoke_device)
```

## API Reference

### Connection

| Function | Description |
|---|---|
| `connect(messaging_urls, zone, credentials, tls_config)` | Initialize messaging connection |
| `disconnect()` | Close connection and release resources |
| `get_connection()` | Get current connection (auto-connects if needed) |

### Hierarchical Discovery Tools

| Function | Description |
|---|---|
| `describe_fleet()` | Bird's-eye summary — device counts by type and location |
| `list_devices(device_type, location, status, group_by, offset, limit)` | Paginated device roster (compact, no schemas) |
| `get_device_functions(device_id)` | Full function schemas and events for one device |
| `invoke_device(device_id, function, params, llm_reasoning)` | Call a function on a device |

### Additional Tools

| Function | Description |
|---|---|
| `invoke_device_with_fallback(device_ids, function, params, llm_reasoning)` | Try multiple devices in order |
| `get_device_status(device_id)` | Get detailed device status |
| `discover_devices(device_type, refresh)` | *(deprecated)* Flat list with full schemas — use the hierarchical tools above |

### Connection Object

For advanced use via `get_connection()`:

| Method | Description |
|---|---|
| `conn.list_devices(device_type, location)` | List devices from registry/D2D |
| `conn.get_device(device_id)` | Get single device by ID |
| `conn.invoke(device_id, function, params)` | Direct JSON-RPC call |
| `conn.async_subscribe(subject, callback)` | Subscribe to messaging subject |
| `conn.messaging_client` | Underlying `MessagingClient` instance |

## Contributing

We welcome contributions! Please open an [issue](https://github.com/arm/device-connect/issues) to report bugs or suggest features, or submit a [pull request](https://github.com/arm/device-connect/pulls) directly.

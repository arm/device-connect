"""Device Connect Tools — framework-agnostic SDK for Device Connect IoT over NATS.

Connect any AI agent to the Device Connect IoT network. No dependency on
device-connect-server or any specific agent framework.

Plain Python (no framework):
    from device_connect_agent_tools import connect, discover_devices, invoke_device

    connect()
    devices = discover_devices()
    result = invoke_device("robot-001", "start_cleaning", {"zone": "A"})

Strands:
    from device_connect_agent_tools import connect
    from device_connect_agent_tools.adapters.strands import discover_devices, invoke_device
    from strands import Agent

    connect()
    agent = Agent(tools=[discover_devices, invoke_device])
    agent("Find all cameras and capture an image")

LangChain:
    from device_connect_agent_tools import connect
    from device_connect_agent_tools.adapters.langchain import discover_devices, invoke_device

    connect()
    agent = create_react_agent(model, [discover_devices, invoke_device])
"""

from device_connect_agent_tools.agent import DeviceConnectAgent
from device_connect_agent_tools.connection import connect, disconnect, get_connection
from device_connect_agent_tools.tools import (
    discover_devices,
    invoke_device,
    invoke_device_with_fallback,
    get_device_status,
)

__all__ = [
    # Connection management
    "connect",
    "disconnect",
    "get_connection",
    # High-level agent
    "DeviceConnectAgent",
    # Tool functions (plain Python — use adapters for framework-specific wrappers)
    "discover_devices",
    "invoke_device",
    "invoke_device_with_fallback",
    "get_device_status",
]

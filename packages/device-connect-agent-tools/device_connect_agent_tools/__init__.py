# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Device Connect Tools — framework-agnostic SDK for Device Connect IoT.

Hierarchical discovery keeps LLM context small:

    from device_connect_agent_tools import connect, describe_fleet, list_devices

    connect()
    fleet = describe_fleet()            # bird's-eye summary (~200 tokens)
    cameras = list_devices(device_type="camera")  # compact roster
    info = get_device_functions("camera-001")     # full schemas for one device
    result = invoke_device("camera-001", "capture_image", {"resolution": "1080p"})

Strands:
    from device_connect_agent_tools import connect
    from device_connect_agent_tools.adapters.strands import (
        describe_fleet, list_devices, get_device_functions, invoke_device,
    )
    from strands import Agent

    connect()
    agent = Agent(tools=[describe_fleet, list_devices, get_device_functions, invoke_device])
"""

from device_connect_agent_tools.agent import DeviceConnectAgent
from device_connect_agent_tools.connection import connect, disconnect, get_connection
from device_connect_agent_tools.tools import (
    describe_fleet,
    list_devices,
    get_device_functions,
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
    # Hierarchical discovery tools (recommended)
    "describe_fleet",
    "list_devices",
    "get_device_functions",
    # Invocation tools
    "invoke_device",
    "invoke_device_with_fallback",
    "get_device_status",
    # Backward-compatible (deprecated — use hierarchical tools instead)
    "discover_devices",
]

# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Device Connect Tools — framework-agnostic SDK for Device Connect IoT.

Selector-driven discovery keeps LLM context small:

    from device_connect_agent_tools import connect, discover, discover_labels

    connect()
    vocab = discover_labels()                                       # fleet vocabulary
    cams = discover("device(category:camera, location:zone-A/*)")  # device roster
    writes = discover("device(*).function(direction:write)")        # function tuples
    result = invoke_device("camera-001", "capture_image", {"resolution": "1080p"})

The older ``describe_fleet`` / ``list_devices`` / ``get_device_functions``
trio remains available for one release as advisory-deprecated wrappers --
prefer ``discover`` / ``discover_labels`` for new code.
"""

from device_connect_agent_tools.agent import DeviceConnectAgent
from device_connect_agent_tools.connection import connect, disconnect, get_connection
from device_connect_agent_tools.tools import (
    # Selector-driven discovery (preferred)
    discover,
    discover_labels,
    # Invocation
    invoke_device,
    invoke_device_with_fallback,
    get_device_status,
    # Advisory-deprecated discovery wrappers (one-release transition)
    describe_fleet,
    list_devices,
    get_device_functions,
    discover_devices,
)

__all__ = [
    # Connection management
    "connect",
    "disconnect",
    "get_connection",
    # High-level agent
    "DeviceConnectAgent",
    # Selector-driven discovery (preferred)
    "discover",
    "discover_labels",
    # Invocation
    "invoke_device",
    "invoke_device_with_fallback",
    "get_device_status",
    # Advisory-deprecated -- use discover() / discover_labels() instead
    "describe_fleet",
    "list_devices",
    "get_device_functions",
    "discover_devices",
]

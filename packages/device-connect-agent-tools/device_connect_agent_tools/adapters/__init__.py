# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Framework adapters for device-connect-agent-tools.

Import tools from the adapter matching your agent framework:

    # Strands
    from device_connect_agent_tools.adapters.strands import (
        describe_fleet, list_devices, get_device_functions, invoke_device,
    )

    # LangChain
    from device_connect_agent_tools.adapters.langchain import (
        describe_fleet, list_devices, get_device_functions, invoke_device,
    )

    # Claude Agent SDK
    from device_connect_agent_tools.adapters.claude import create_device_connect_server
"""

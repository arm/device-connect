# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for DeviceConnectMCP capability construction."""

from __future__ import annotations

from device_connect_agent_tools.mcp import DeviceConnectMCP


def test_capabilities_build_with_event_name_token() -> None:
    mcp = DeviceConnectMCP("sensor-01")

    @mcp.event()
    async def work_done(task_id: str) -> None:
        """Work completed."""

    capabilities = mcp.get_capabilities()

    assert len(capabilities.events) == 1
    assert capabilities.events[0].name == "work_done"

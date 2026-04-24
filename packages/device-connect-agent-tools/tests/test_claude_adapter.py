# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for device_connect_agent_tools.adapters.claude module.

Validates that the Claude Agent SDK adapter wraps Device Connect tool functions
with @claude_agent_sdk.tool and bundles them into an in-process MCP server.
"""

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest


def _install_claude_sdk_mock():
    """Install a minimal mock of claude_agent_sdk before importing the adapter."""
    sdk_mod = ModuleType("claude_agent_sdk")

    def fake_tool(name, description, schema):
        """Mock @tool(name, desc, schema) — returns a decorator that tags the handler."""

        def decorator(fn):
            wrapped = MagicMock(wraps=fn, __name__=name, __doc__=description)
            wrapped._tool_name = name
            wrapped._tool_schema = schema
            wrapped.__wrapped__ = fn
            return wrapped

        return decorator

    def fake_create_sdk_mcp_server(*args, **kwargs):
        return {
            "name": kwargs.get("name") or (args[0] if args else None),
            "tools": kwargs.get("tools", []),
        }

    sdk_mod.tool = fake_tool
    sdk_mod.create_sdk_mcp_server = fake_create_sdk_mcp_server
    sys.modules["claude_agent_sdk"] = sdk_mod
    return sdk_mod


@pytest.fixture(autouse=True)
def _mock_sdk_and_connection():
    """Mock claude_agent_sdk and the Device Connect connection for all tests."""
    _install_claude_sdk_mock()

    mock_conn = MagicMock()
    mock_conn.list_devices.return_value = []
    mock_conn.invoke.return_value = {"jsonrpc": "2.0", "id": "1", "result": {}}
    mock_conn.get_device.return_value = {"device_id": "dev-1", "device_type": "test"}

    with patch(
        "device_connect_agent_tools.tools.get_connection", return_value=mock_conn
    ):
        for mod_name in list(sys.modules):
            if "device_connect_agent_tools.adapters.claude" in mod_name:
                del sys.modules[mod_name]
        yield mock_conn

    sys.modules.pop("claude_agent_sdk", None)


TOOL_NAMES = (
    "describe_fleet",
    "list_devices",
    "get_device_functions",
    "discover_devices",
    "invoke_device",
    "invoke_device_with_fallback",
    "get_device_status",
)


class TestClaudeAdapterExports:
    def test_module_exports_all_tools(self):
        from device_connect_agent_tools.adapters import claude as adapter

        for name in TOOL_NAMES:
            assert hasattr(adapter, name), f"Missing export: {name}"
        assert hasattr(adapter, "create_device_connect_server")

    def test_all_list(self):
        from device_connect_agent_tools.adapters import claude as adapter

        expected = set(TOOL_NAMES) | {"create_device_connect_server"}
        assert set(adapter.__all__) == expected

    def test_tools_are_callable(self):
        from device_connect_agent_tools.adapters import claude as adapter

        for name in TOOL_NAMES:
            assert callable(getattr(adapter, name)), f"{name} is not callable"

    def test_tool_names_match(self):
        from device_connect_agent_tools.adapters import claude as adapter

        for name in TOOL_NAMES:
            assert getattr(adapter, name)._tool_name == name

    def test_create_server_bundles_all_tools(self):
        from device_connect_agent_tools.adapters import claude as adapter

        server = adapter.create_device_connect_server()
        assert server["name"] == "device-connect"
        bundled = {t._tool_name for t in server["tools"]}
        assert bundled == set(TOOL_NAMES)

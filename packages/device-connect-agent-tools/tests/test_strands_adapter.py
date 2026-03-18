"""Unit tests for device_connect_agent_tools.adapters.strands module.

Validates that the Strands adapter correctly wraps Device Connect tool functions
with @strands.tool, producing callable tools with correct names.
"""

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest


def _install_strands_mock():
    """Install a minimal mock of the strands package before importing the adapter."""
    strands_mod = ModuleType("strands")

    def fake_tool(fn):
        """Mock @strands.tool that preserves the function and adds metadata."""
        wrapped = MagicMock(wraps=fn, __name__=fn.__name__, __doc__=fn.__doc__)
        wrapped._original = fn
        wrapped.__wrapped__ = fn
        return wrapped

    strands_mod.tool = fake_tool
    sys.modules["strands"] = strands_mod
    return strands_mod


@pytest.fixture(autouse=True)
def _mock_strands_and_connection():
    """Mock the strands package and the Device Connect connection for all tests."""
    strands_mod = _install_strands_mock()

    mock_conn = MagicMock()
    mock_conn.list_devices.return_value = []
    mock_conn.invoke.return_value = {"jsonrpc": "2.0", "id": "1", "result": {}}
    mock_conn.get_device.return_value = {"device_id": "dev-1", "device_type": "test"}

    with patch("device_connect_agent_tools.tools.get_connection", return_value=mock_conn):
        # Force reimport of the adapter so it picks up our mock
        for mod_name in list(sys.modules):
            if "device_connect_agent_tools.adapters.strands" in mod_name:
                del sys.modules[mod_name]
        yield mock_conn

    # Cleanup strands mock
    sys.modules.pop("strands", None)


class TestStrandsAdapterExports:
    def test_module_exports_four_tools(self):
        from device_connect_agent_tools.adapters import strands as adapter

        assert hasattr(adapter, "discover_devices")
        assert hasattr(adapter, "invoke_device")
        assert hasattr(adapter, "invoke_device_with_fallback")
        assert hasattr(adapter, "get_device_status")

    def test_all_list(self):
        from device_connect_agent_tools.adapters import strands as adapter

        expected = {"discover_devices", "invoke_device", "invoke_device_with_fallback", "get_device_status"}
        assert set(adapter.__all__) == expected

    def test_tools_are_callable(self):
        from device_connect_agent_tools.adapters import strands as adapter

        assert callable(adapter.discover_devices)
        assert callable(adapter.invoke_device)
        assert callable(adapter.invoke_device_with_fallback)
        assert callable(adapter.get_device_status)

    def test_tool_names_match(self):
        from device_connect_agent_tools.adapters import strands as adapter

        assert adapter.discover_devices.__name__ == "discover_devices"
        assert adapter.invoke_device.__name__ == "invoke_device"
        assert adapter.invoke_device_with_fallback.__name__ == "invoke_device_with_fallback"
        assert adapter.get_device_status.__name__ == "get_device_status"

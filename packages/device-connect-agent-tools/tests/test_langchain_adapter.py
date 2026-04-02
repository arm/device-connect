"""Unit tests for device_connect_agent_tools.adapters.langchain module.

Validates that the LangChain adapter correctly wraps Device Connect tool functions
as StructuredTool instances with correct names and descriptions.
"""

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest


def _install_langchain_mock():
    """Install a minimal mock of langchain_core.tools before importing the adapter."""
    # Build the langchain_core.tools module hierarchy
    langchain_core = ModuleType("langchain_core")
    langchain_core_tools = ModuleType("langchain_core.tools")

    class FakeStructuredTool:
        """Mock StructuredTool that captures from_function calls."""

        def __init__(self, name, description, func):
            self.name = name
            self.description = description
            self._func = func

        @classmethod
        def from_function(cls, func, **kwargs):
            return cls(
                name=func.__name__,
                description=(func.__doc__ or "").strip().split("\n")[0],
                func=func,
            )

    langchain_core_tools.StructuredTool = FakeStructuredTool
    langchain_core.tools = langchain_core_tools

    sys.modules["langchain_core"] = langchain_core
    sys.modules["langchain_core.tools"] = langchain_core_tools

    return FakeStructuredTool


@pytest.fixture(autouse=True)
def _mock_langchain_and_connection():
    """Mock langchain_core and the Device Connect connection for all tests."""
    FakeStructuredTool = _install_langchain_mock()

    mock_conn = MagicMock()
    mock_conn.list_devices.return_value = []
    mock_conn.invoke.return_value = {"jsonrpc": "2.0", "id": "1", "result": {}}
    mock_conn.get_device.return_value = {"device_id": "dev-1", "device_type": "test"}

    with patch("device_connect_agent_tools.tools.get_connection", return_value=mock_conn):
        # Force reimport of the adapter so it picks up our mock
        for mod_name in list(sys.modules):
            if "device_connect_agent_tools.adapters.langchain" in mod_name:
                del sys.modules[mod_name]
        yield mock_conn, FakeStructuredTool

    # Cleanup langchain mocks
    for key in list(sys.modules):
        if key.startswith("langchain_core"):
            del sys.modules[key]


class TestLangchainAdapterExports:
    def test_module_exports_four_tools(self):
        from device_connect_agent_tools.adapters import langchain as adapter

        assert hasattr(adapter, "discover_devices")
        assert hasattr(adapter, "invoke_device")
        assert hasattr(adapter, "invoke_device_with_fallback")
        assert hasattr(adapter, "get_device_status")

    def test_all_list(self):
        from device_connect_agent_tools.adapters import langchain as adapter

        expected = {"discover_devices", "invoke_device", "invoke_device_with_fallback", "get_device_status", "list_devices", "get_device_functions", "describe_fleet"}
        assert set(adapter.__all__) == expected

    def test_tools_are_structured_tool_instances(self):
        from device_connect_agent_tools.adapters import langchain as adapter

        assert type(adapter.discover_devices).__name__ == "FakeStructuredTool"
        assert type(adapter.invoke_device).__name__ == "FakeStructuredTool"
        assert type(adapter.invoke_device_with_fallback).__name__ == "FakeStructuredTool"
        assert type(adapter.get_device_status).__name__ == "FakeStructuredTool"

    def test_tool_names_match(self):
        from device_connect_agent_tools.adapters import langchain as adapter

        assert adapter.discover_devices.name == "discover_devices"
        assert adapter.invoke_device.name == "invoke_device"
        assert adapter.invoke_device_with_fallback.name == "invoke_device_with_fallback"
        assert adapter.get_device_status.name == "get_device_status"

    def test_tool_descriptions_not_empty(self):
        from device_connect_agent_tools.adapters import langchain as adapter

        assert len(adapter.discover_devices.description) > 0
        assert len(adapter.invoke_device.description) > 0
        assert len(adapter.invoke_device_with_fallback.description) > 0
        assert len(adapter.get_device_status.description) > 0

"""MCP tool name parsing utilities."""

from __future__ import annotations


def parse_tool_name(tool_name: str) -> tuple[str, str]:
    """Parse MCP tool name into device_id and function_name.

    Args:
        tool_name: Tool name in format "{device_id}::{function_name}"

    Returns:
        Tuple of (device_id, function_name)

    Raises:
        ValueError: If tool name format is invalid
    """
    if "::" not in tool_name:
        raise ValueError(
            f"Invalid tool name format: '{tool_name}'. "
            f"Expected format: 'device_id::function_name'"
        )

    parts = tool_name.split("::", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(
            f"Invalid tool name format: '{tool_name}'. "
            f"Expected format: 'device_id::function_name'"
        )

    return parts[0], parts[1]

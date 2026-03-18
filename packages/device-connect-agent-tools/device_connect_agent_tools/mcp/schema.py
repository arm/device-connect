"""Schema conversion between Device Connect and MCP formats.

Both Device Connect and MCP use JSON Schema for function/tool parameters,
so conversion is straightforward. This module provides utilities for:
- Converting Device Connect FunctionDef to MCP tool schema
- Validating MCP arguments against schemas
- Converting device results to MCP responses
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from device_connect_sdk.types import FunctionDef


@dataclass
class MCPToolDefinition:
    """MCP tool definition matching the MCP protocol spec.

    Attributes:
        name: Tool name in format "{device_id}::{function_name}"
        description: Human-readable description for LLM
        input_schema: JSON Schema for tool parameters
    """

    name: str
    description: str
    input_schema: Dict[str, Any]

    def to_mcp_dict(self) -> Dict[str, Any]:
        """Convert to MCP protocol format."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


def function_to_mcp_tool(
    device_id: str,
    function_def: FunctionDef,
    device_type: Optional[str] = None,
    device_location: Optional[str] = None,
) -> MCPToolDefinition:
    """Convert Device Connect FunctionDef to MCP tool definition.

    The tool name follows the pattern: "{device_id}::{function_name}"
    The description includes device context for better LLM understanding.

    Args:
        device_id: Device identifier
        function_def: Device Connect function definition
        device_type: Optional device type for context
        device_location: Optional device location for context

    Returns:
        MCP tool definition

    Example:
        >>> func = FunctionDef(
        ...     name="capture_image",
        ...     description="Capture an image from the camera",
        ...     parameters={"type": "object", "properties": {...}}
        ... )
        >>> tool = function_to_mcp_tool("camera-001", func, "camera")
        >>> tool.name
        'camera-001::capture_image'
    """
    # Build tool name
    tool_name = f"{device_id}::{function_def.name}"

    # Build description with device context
    desc_parts = []
    if device_type:
        desc_parts.append(f"[{device_type}]")
    if device_location:
        desc_parts.append(f"@{device_location}")
    if desc_parts:
        desc_parts.append("-")
    desc_parts.append(function_def.description or f"Call {function_def.name}")

    description = " ".join(desc_parts)

    # Use parameters directly (already JSON Schema)
    input_schema = function_def.parameters or {
        "type": "object",
        "properties": {},
        "required": [],
    }

    return MCPToolDefinition(
        name=tool_name,
        description=description,
        input_schema=input_schema,
    )


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


def mcp_arguments_to_params(
    arguments: Dict[str, Any],
    parameter_schema: Dict[str, Any],
) -> Dict[str, Any]:
    """Convert MCP tool arguments to device function parameters.

    Currently a pass-through since both use the same format,
    but provides a hook for future validation or transformation.

    Args:
        arguments: Arguments from MCP tool call
        parameter_schema: JSON Schema for validation

    Returns:
        Parameters for device function call
    """
    return arguments


def device_result_to_mcp_response(result: Any) -> Dict[str, Any]:
    """Convert device function result to MCP tool response.

    Args:
        result: Result from device function

    Returns:
        MCP-formatted response
    """
    # If result is already a dict, use it directly
    if isinstance(result, dict):
        return result

    # Wrap primitive values
    return {"result": result}


def devices_to_mcp_tools(
    devices: List[Dict[str, Any]],
) -> List[MCPToolDefinition]:
    """Convert list of device registration data to MCP tools.

    Args:
        devices: List of device data from registry discovery

    Returns:
        List of MCP tool definitions
    """
    tools = []

    for device in devices:
        device_id = device.get("device_id", "unknown")
        device_type = device.get("identity", {}).get("device_type")
        device_location = device.get("status", {}).get("location")

        # Get capabilities
        capabilities = device.get("capabilities", {})
        functions = capabilities.get("functions", [])

        for func_data in functions:
            # Convert to FunctionDef if dict
            if isinstance(func_data, dict):
                func_def = FunctionDef(
                    name=func_data.get("name", "unknown"),
                    description=func_data.get("description", ""),
                    parameters=func_data.get("parameters", {}),
                    tags=func_data.get("tags", []),
                )
            else:
                func_def = func_data

            tool = function_to_mcp_tool(
                device_id=device_id,
                function_def=func_def,
                device_type=device_type,
                device_location=device_location,
            )
            tools.append(tool)

    return tools

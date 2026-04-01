"""Tool router for MCP Bridge.

Routes MCP tool calls to Device Connect devices via NATS JSON-RPC.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, Optional

from device_connect_sdk.messaging.base import MessagingClient
from device_connect_agent_tools.mcp.schema import parse_tool_name

logger = logging.getLogger(__name__)


class ToolRouter:
    """Routes MCP tool calls to Device Connect devices via NATS.

    Handles:
    - Parsing tool names (device_id::function_name)
    - Building JSON-RPC requests
    - Sending requests to device command subjects
    - Processing responses

    Example:
        router = ToolRouter(messaging_client, tenant="default")
        result = await router.invoke("camera-001::capture_image", {"resolution": "1080p"})
    """

    def __init__(
        self,
        messaging_client: MessagingClient,
        tenant: str = "default",
        timeout: float = 30.0,
    ):
        """Initialize tool router.

        Args:
            messaging_client: Connected messaging client (NATS)
            tenant: Device Connect tenant name
            timeout: Request timeout in seconds
        """
        self._client = messaging_client
        self._tenant = tenant
        self._timeout = timeout

    async def invoke(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        timeout: Optional[float] = None,
    ) -> Any:
        """Invoke a device function via NATS JSON-RPC.

        Args:
            tool_name: Tool name in format "device_id::function_name"
            arguments: Function arguments
            timeout: Optional override for request timeout

        Returns:
            Function result

        Raises:
            ToolInvocationError: If invocation fails
            ToolNotFoundError: If device or function not found
        """
        # Parse tool name
        try:
            device_id, function_name = parse_tool_name(tool_name)
        except ValueError as e:
            raise ToolInvocationError(str(e)) from e

        logger.info(f"Invoking {device_id}::{function_name}")
        logger.debug(f"Arguments: {arguments}")

        # Build JSON-RPC request
        request_id = str(uuid.uuid4())
        request = {
            "jsonrpc": "2.0",
            "method": function_name,
            "params": arguments,
            "id": request_id,
        }

        # Send to device command subject
        subject = f"device-connect.{self._tenant}.{device_id}.cmd"
        effective_timeout = timeout or self._timeout

        try:
            logger.debug(f"Sending request to {subject}")
            response_data = await self._client.request(
                subject,
                json.dumps(request).encode(),
                timeout=effective_timeout,
            )

            response = json.loads(response_data.decode())

            # Check for JSON-RPC error
            if "error" in response:
                error = response["error"]
                code = error.get("code", -32000)
                message = error.get("message", "Unknown error")

                if code == -32601:  # Method not found
                    raise ToolNotFoundError(
                        f"Function '{function_name}' not found on device '{device_id}'"
                    )

                raise ToolInvocationError(
                    f"Device error: {message}",
                    device_id=device_id,
                    function_name=function_name,
                    error_code=code,
                )

            # Return result
            result = response.get("result")
            logger.debug(f"Result: {result}")
            return result

        except TimeoutError as e:
            logger.error(f"Timeout invoking {tool_name}")
            raise ToolInvocationError(
                f"Timeout waiting for response from device '{device_id}'",
                device_id=device_id,
                function_name=function_name,
            ) from e

        except Exception as e:
            if isinstance(e, (ToolInvocationError, ToolNotFoundError)):
                raise
            logger.error(f"Error invoking {tool_name}: {e}")
            raise ToolInvocationError(
                f"Failed to invoke {tool_name}: {e}",
                device_id=device_id,
                function_name=function_name,
            ) from e


class ToolInvocationError(Exception):
    """Raised when tool invocation fails."""

    def __init__(
        self,
        message: str,
        device_id: Optional[str] = None,
        function_name: Optional[str] = None,
        error_code: Optional[int] = None,
    ):
        super().__init__(message)
        self.device_id = device_id
        self.function_name = function_name
        self.error_code = error_code


class ToolNotFoundError(ToolInvocationError):
    """Raised when the requested tool (device/function) is not found."""

    pass

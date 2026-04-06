"""MCP server for remote device operations.

This module provides an MCP server that exposes tools for interacting with
remote Device Connect devices in Setup mode. It's designed to be used with
Claude Code for capability generation.

Usage:
    Add to Claude Code's .claude.json:
    {
      "mcpServers": {
        "device-connect-devices": {
          "type": "stdio",
          "command": "python",
          "args": ["-m", "device_connect_agent_tools.mcp.device_tools"],
          "env": {
            "NATS_URL": "nats://localhost:4222",
            "DEVICE_CONNECT_TENANT": "default"
          }
        }
      }
    }

    Then use tools like:
    - list_devices() - discover devices in Setup mode
    - device_introspect(device_id) - get device context
    - device_read(device_id, path) - read file from device
    - device_write(device_id, path, content) - write file to device
    - etc.
"""
import asyncio
import uuid
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Environment configuration
NATS_URL = os.getenv("NATS_URL", "nats://localhost:4222")
DEVICE_CONNECT_TENANT = os.getenv("DEVICE_CONNECT_TENANT", "default")
DEVICE_CONNECT_ALLOW_INSECURE = os.getenv("DEVICE_CONNECT_ALLOW_INSECURE", "false").lower() == "true"

# Configurable path to reference capability packs.
# Defaults to the device-connect-capability-agent repo next to device-connect-agent-tools,
# or checks well-known sibling directory locations.
CAPABILITY_PACKS_PATH = os.getenv("DEVICE_CONNECT_CAPABILITY_PACKS_PATH")

# MCP protocol types
MCP_TOOLS = []


def _resolve_packs_base() -> Optional[Path]:
    """Resolve the reference capability packs base directory."""
    if CAPABILITY_PACKS_PATH:
        p = Path(CAPABILITY_PACKS_PATH)
        if p.is_dir():
            return p

    # Try well-known locations relative to this package
    pkg_root = Path(__file__).resolve().parent.parent.parent.parent
    candidates = [
        pkg_root / "device-connect-capability-agent" / "reference_capability_packs",
        pkg_root / "core" / "reference_capability_packs",
    ]
    for c in candidates:
        if c.is_dir():
            return c
    return None


def mcp_tool(name: str, description: str, parameters: dict):
    """Decorator to register a function as an MCP tool."""
    def decorator(func):
        func._mcp_tool = {
            "name": name,
            "description": description,
            "inputSchema": {
                "type": "object",
                "properties": parameters,
                "required": [k for k, v in parameters.items() if "default" not in str(v)]
            }
        }
        MCP_TOOLS.append(func)
        return func
    return decorator


class DeviceToolsServer:
    """MCP server for device tools."""

    def __init__(self):
        self._messaging = None
        self._connected = False

    async def connect(self) -> None:
        """Connect to NATS."""
        if self._connected:
            return

        try:
            from device_connect_edge.messaging import create_client

            self._messaging = create_client("nats")
            connect_opts = {"servers": [NATS_URL]}

            if DEVICE_CONNECT_ALLOW_INSECURE:
                connect_opts["allow_reconnect"] = True

            await self._messaging.connect(**connect_opts)
            self._connected = True
            logger.info(f"Connected to NATS at {NATS_URL}")
        except Exception as e:
            logger.error(f"Failed to connect to NATS: {e}")
            raise

    async def disconnect(self) -> None:
        """Disconnect from NATS."""
        if self._messaging and self._connected:
            await self._messaging.close()
            self._connected = False

    async def send_command(self, device_id: str, command: dict, timeout: float = 30.0) -> dict:
        """Send a setup command to a device.

        Args:
            device_id: Target device ID
            command: Command dict with "tool" and parameters
            timeout: Request timeout in seconds

        Returns:
            Response dict from device
        """
        await self.connect()

        subject = f"device-connect.{DEVICE_CONNECT_TENANT}.{device_id}.setup.cmd"
        try:
            response = await self._messaging.request(
                subject,
                json.dumps(command).encode(),
                timeout=timeout
            )
            # MessagingClient returns bytes directly, not a response object
            return json.loads(response.decode())
        except asyncio.TimeoutError:
            return {"error": f"Timeout waiting for response from {device_id}"}
        except Exception as e:
            return {"error": str(e)}

    # --- MCP Tools ---

    @mcp_tool(
        name="list_devices",
        description="List all devices in Setup mode that can be programmed",
        parameters={}
    )
    async def list_devices(self) -> dict:
        """List devices in Setup mode using scatter-gather discovery."""
        await self.connect()

        # Use scatter-gather to discover all setup-mode devices
        # Each device subscribed to device-connect.{tenant}.setup.discover will respond
        devices = []
        inbox = f"_INBOX.{uuid.uuid4().hex}"
        discovery_timeout = 2.0  # seconds to wait for responses

        try:
            async def on_response(data: bytes, reply: str = None):
                try:
                    result = json.loads(data.decode())
                    if "device_id" in result and result.get("device_id") != "unknown":
                        devices.append(result)
                except Exception:
                    logger.warning("Failed to parse discovery response", exc_info=True)

            # Subscribe to inbox to collect responses
            sub = await self._messaging.subscribe(inbox, callback=on_response)

            # Send discovery request to all setup-mode devices
            # Use the underlying NATS client to publish with reply header
            await self._messaging._nc.publish(
                f"device-connect.{DEVICE_CONNECT_TENANT}.setup.discover",
                json.dumps({"tool": "Discover"}).encode(),
                reply=inbox
            )

            # Wait for responses
            await asyncio.sleep(discovery_timeout)

            # Unsubscribe
            await sub.unsubscribe()

            if devices:
                return {"devices": devices}
            else:
                return {"devices": [], "note": "No setup-mode devices responded. Make sure devices are running with --setup-mode flag."}

        except Exception as e:
            return {"error": f"Discovery failed: {e}"}

    @mcp_tool(
        name="device_introspect",
        description="Get device identity, hardware, and current capabilities",
        parameters={
            "device_id": {"type": "string", "description": "Target device ID"}
        }
    )
    async def device_introspect(self, device_id: str) -> dict:
        """Introspect a device."""
        return await self.send_command(device_id, {"tool": "Introspect"})

    @mcp_tool(
        name="device_read",
        description="Read a file from a device",
        parameters={
            "device_id": {"type": "string", "description": "Target device ID"},
            "path": {"type": "string", "description": "File path relative to device root"}
        }
    )
    async def device_read(self, device_id: str, path: str) -> dict:
        """Read a file from a device."""
        return await self.send_command(device_id, {"tool": "Read", "path": path})

    @mcp_tool(
        name="device_write",
        description="Write a file to a device",
        parameters={
            "device_id": {"type": "string", "description": "Target device ID"},
            "path": {"type": "string", "description": "File path relative to device root"},
            "content": {"type": "string", "description": "File content to write"}
        }
    )
    async def device_write(self, device_id: str, path: str, content: str) -> dict:
        """Write a file to a device."""
        return await self.send_command(device_id, {"tool": "Write", "path": path, "content": content})

    @mcp_tool(
        name="device_edit",
        description="Edit a file on a device (search/replace)",
        parameters={
            "device_id": {"type": "string", "description": "Target device ID"},
            "path": {"type": "string", "description": "File path relative to device root"},
            "old_string": {"type": "string", "description": "String to find"},
            "new_string": {"type": "string", "description": "String to replace with"},
            "replace_all": {"type": "boolean", "description": "Replace all occurrences", "default": False}
        }
    )
    async def device_edit(
        self,
        device_id: str,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False
    ) -> dict:
        """Edit a file on a device."""
        return await self.send_command(device_id, {
            "tool": "Edit",
            "path": path,
            "old_string": old_string,
            "new_string": new_string,
            "replace_all": replace_all
        })

    @mcp_tool(
        name="device_glob",
        description="Find files on a device by glob pattern",
        parameters={
            "device_id": {"type": "string", "description": "Target device ID"},
            "pattern": {"type": "string", "description": "Glob pattern (e.g., 'capabilities/**/*.py')"}
        }
    )
    async def device_glob(self, device_id: str, pattern: str) -> dict:
        """Find files on a device."""
        return await self.send_command(device_id, {"tool": "Glob", "pattern": pattern})

    @mcp_tool(
        name="device_grep",
        description="Search file contents on a device",
        parameters={
            "device_id": {"type": "string", "description": "Target device ID"},
            "pattern": {"type": "string", "description": "Regex pattern to search for"},
            "path": {"type": "string", "description": "Path to search in", "default": "."},
            "recursive": {"type": "boolean", "description": "Search recursively", "default": True}
        }
    )
    async def device_grep(
        self,
        device_id: str,
        pattern: str,
        path: str = ".",
        recursive: bool = True
    ) -> dict:
        """Search file contents on a device."""
        return await self.send_command(device_id, {
            "tool": "Grep",
            "pattern": pattern,
            "path": path,
            "recursive": recursive
        })

    @mcp_tool(
        name="device_bash",
        description="Execute a shell command on a device",
        parameters={
            "device_id": {"type": "string", "description": "Target device ID"},
            "command": {"type": "string", "description": "Shell command to execute"},
            "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 30}
        }
    )
    async def device_bash(
        self,
        device_id: str,
        command: str,
        timeout: int = 30
    ) -> dict:
        """Execute a shell command on a device."""
        return await self.send_command(
            device_id,
            {"tool": "Bash", "command": command, "timeout": timeout},
            timeout=timeout + 5
        )

    @mcp_tool(
        name="device_python",
        description="Run Python code or pytest on a device",
        parameters={
            "device_id": {"type": "string", "description": "Target device ID"},
            "code": {"type": "string", "description": "Python code to run (optional)"},
            "file": {"type": "string", "description": "Python file to run (optional)"},
            "pytest": {"type": "string", "description": "Pytest path to run (optional)"},
            "args": {"type": "array", "items": {"type": "string"}, "description": "Additional arguments"},
            "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 60}
        }
    )
    async def device_python(
        self,
        device_id: str,
        code: Optional[str] = None,
        file: Optional[str] = None,
        pytest: Optional[str] = None,
        args: Optional[List[str]] = None,
        timeout: int = 60
    ) -> dict:
        """Run Python code on a device."""
        cmd = {"tool": "Python", "timeout": timeout}
        if code:
            cmd["code"] = code
        if file:
            cmd["file"] = file
        if pytest:
            cmd["pytest"] = pytest
        if args:
            cmd["args"] = args

        return await self.send_command(device_id, cmd, timeout=timeout + 5)

    @mcp_tool(
        name="device_invoke",
        description="Invoke an RPC function on a device in running or canary mode. Use this for integration testing.",
        parameters={
            "device_id": {"type": "string", "description": "Target device ID"},
            "method": {"type": "string", "description": "Function name to invoke (e.g., 'simulate_mess')"},
            "params": {"type": "object", "description": "Function parameters as key-value pairs", "default": {}},
            "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 10}
        }
    )
    async def device_invoke(
        self,
        device_id: str,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: int = 10
    ) -> dict:
        """Invoke an RPC function on a device.

        This sends a JSON-RPC request to the device's command subject
        (device-connect.{tenant}.{device_id}.cmd) for running/canary mode functions.

        Args:
            device_id: Target device ID
            method: Function name to invoke
            params: Function parameters
            timeout: Request timeout in seconds

        Returns:
            JSON-RPC response from device
        """
        await self.connect()

        subject = f"device-connect.{DEVICE_CONNECT_TENANT}.{device_id}.cmd"
        request = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params or {}
        }

        try:
            response = await self._messaging.request(
                subject,
                json.dumps(request).encode(),
                timeout=timeout
            )
            return json.loads(response.decode())
        except asyncio.TimeoutError:
            return {"error": f"Timeout invoking {method} on {device_id}"}
        except Exception as e:
            return {"error": str(e)}

    # --- Orchestrator Query Tools ---

    @mcp_tool(
        name="orch_list_subscriptions",
        description="List all event subscriptions the orchestrator currently has. Use this to verify the orchestrator is subscribed to device events during integration testing.",
        parameters={}
    )
    async def orch_list_subscriptions(self) -> dict:
        """List current orchestrator subscriptions.

        Returns:
            Dict with 'subscriptions' list containing {device_id, event_name} pairs
        """
        await self.connect()

        subject = f"device-connect.{DEVICE_CONNECT_TENANT}.orchestrator.query"
        request = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "listSubscriptions",
            "params": {}
        }

        try:
            response = await self._messaging.request(
                subject,
                json.dumps(request).encode(),
                timeout=5.0
            )
            result = json.loads(response.decode())
            if "error" in result:
                return {"error": result["error"].get("message", "Unknown error")}
            return result.get("result", {})
        except asyncio.TimeoutError:
            return {"error": "Orchestrator not responding. Is it running?"}
        except Exception as e:
            return {"error": str(e)}

    @mcp_tool(
        name="orch_get_recent_events",
        description="Get recent events received by the orchestrator. Use this to verify events are flowing from devices to the orchestrator during integration testing.",
        parameters={
            "limit": {"type": "integer", "description": "Max number of events to return", "default": 20}
        }
    )
    async def orch_get_recent_events(self, limit: int = 20) -> dict:
        """Get recent events received by orchestrator.

        Args:
            limit: Maximum number of events to return

        Returns:
            Dict with 'events' list containing recent event records
        """
        await self.connect()

        subject = f"device-connect.{DEVICE_CONNECT_TENANT}.orchestrator.query"
        request = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "getRecentEvents",
            "params": {"limit": limit}
        }

        try:
            response = await self._messaging.request(
                subject,
                json.dumps(request).encode(),
                timeout=5.0
            )
            result = json.loads(response.decode())
            if "error" in result:
                return {"error": result["error"].get("message", "Unknown error")}
            return result.get("result", {})
        except asyncio.TimeoutError:
            return {"error": "Orchestrator not responding. Is it running?"}
        except Exception as e:
            return {"error": str(e)}

    @mcp_tool(
        name="orch_check_subscription",
        description="Check if the orchestrator is subscribed to a specific device event.",
        parameters={
            "device_id": {"type": "string", "description": "Device ID to check"},
            "event_name": {"type": "string", "description": "Event name to check"}
        }
    )
    async def orch_check_subscription(self, device_id: str, event_name: str) -> dict:
        """Check if orchestrator is subscribed to a specific event.

        Args:
            device_id: Device ID
            event_name: Event name

        Returns:
            Dict with 'subscribed' boolean
        """
        await self.connect()

        subject = f"device-connect.{DEVICE_CONNECT_TENANT}.orchestrator.query"
        request = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "isSubscribed",
            "params": {"device_id": device_id, "event_name": event_name}
        }

        try:
            response = await self._messaging.request(
                subject,
                json.dumps(request).encode(),
                timeout=5.0
            )
            result = json.loads(response.decode())
            if "error" in result:
                return {"error": result["error"].get("message", "Unknown error")}
            return result.get("result", {})
        except asyncio.TimeoutError:
            return {"error": "Orchestrator not responding. Is it running?"}
        except Exception as e:
            return {"error": str(e)}

    # --- Reference Pack Tools ---

    @mcp_tool(
        name="copy_entire_capability_to_device",
        description="Copy an ENTIRE reference capability pack to a device. Copies ALL files from reference_capability_packs/{pack}/ to device capabilities/{name}/. Use this rarely - prefer copy_file_to_device for individual binary assets.",
        parameters={
            "device_id": {"type": "string", "description": "Target device ID"},
            "reference_pack": {"type": "string", "description": "Name of reference pack (e.g., 'vlm_detection', 'mess_cleanup', 'zone_monitoring')"},
            "capability_name": {"type": "string", "description": "Name for the capability on device (defaults to reference_pack name)"},
            "config_overrides": {"type": "object", "description": "Key-value pairs to override in manifest.json"}
        }
    )
    async def copy_entire_capability_to_device(
        self,
        device_id: str,
        reference_pack: str,
        capability_name: Optional[str] = None,
        config_overrides: Optional[Dict[str, Any]] = None
    ) -> dict:
        """Copy a reference capability pack to a device.

        This tool copies all files from a local reference capability pack to a
        remote device's capabilities directory. Use this when the reference pack
        matches the device hardware and only configuration changes are needed.

        Args:
            device_id: Target device ID
            reference_pack: Name of the reference pack directory
            capability_name: Name for the capability on device (defaults to reference_pack)
            config_overrides: Key-value pairs to merge into manifest.json

        Returns:
            Dict with success status and list of copied files
        """
        import base64

        base_path = _resolve_packs_base()
        if base_path is None:
            return {
                "error": "Reference capability packs not found. "
                         "Set DEVICE_CONNECT_CAPABILITY_PACKS_PATH environment variable."
            }

        pack_path = base_path / reference_pack

        if not pack_path.exists():
            available = [d.name for d in base_path.iterdir() if d.is_dir() and not d.name.startswith(".")]
            return {
                "error": f"Reference pack '{reference_pack}' not found",
                "available_packs": available
            }

        target_name = capability_name or reference_pack
        files_copied = []

        # Copy all files recursively
        for file_path in pack_path.rglob("*"):
            if file_path.is_file():
                rel_path = file_path.relative_to(pack_path)
                target_path = f"capabilities/{target_name}/{rel_path}"

                is_binary = False
                try:
                    content = file_path.read_text()
                except UnicodeDecodeError:
                    # Binary file - read as bytes and base64 encode
                    binary_data = file_path.read_bytes()
                    content = base64.b64encode(binary_data).decode('ascii')
                    is_binary = True
                    logger.info(f"Copying binary file: {rel_path}")

                # Apply config overrides to manifest.json
                if rel_path.name == "manifest.json" and config_overrides:
                    manifest = json.loads(content)
                    manifest.update(config_overrides)
                    if "id" not in config_overrides:
                        manifest["id"] = target_name
                    content = json.dumps(manifest, indent=2)

                command = {
                    "tool": "Write",
                    "path": target_path,
                    "content": content
                }
                if is_binary:
                    command["binary"] = True

                result = await self.send_command(device_id, command)

                if "error" in result:
                    return {"error": f"Failed to write {target_path}: {result['error']}"}

                files_copied.append(target_path)

        return {
            "success": True,
            "reference_pack": reference_pack,
            "capability_name": target_name,
            "files_copied": files_copied
        }

    @mcp_tool(
        name="copy_file_to_device",
        description="Copy a single file from reference_capability_packs/ to a device. Use this for binary assets (sounds, images, model weights) that cannot be transferred via device_write.",
        parameters={
            "device_id": {"type": "string", "description": "Target device ID"},
            "reference_pack": {"type": "string", "description": "Reference pack name containing the file (e.g., 'vlm_detection')"},
            "file_path": {"type": "string", "description": "Path to file within the pack (e.g., 'Futuristic_Ping_A.wav' or 'assets/sound.wav')"},
            "remote_path": {"type": "string", "description": "Destination path on device (e.g., 'capabilities/vlm/sound.wav')"}
        }
    )
    async def copy_file_to_device(
        self,
        device_id: str,
        reference_pack: str,
        file_path: str,
        remote_path: str
    ) -> dict:
        """Copy a single file from a reference capability pack to a device.

        This tool copies a single file from a local reference pack to a remote
        device. Use this for binary assets (sounds, images, model weights) that
        cannot be passed through device_write's content parameter.

        Args:
            device_id: Target device ID
            reference_pack: Name of the reference pack directory
            file_path: Path to the file within the pack
            remote_path: Destination path on the device

        Returns:
            Dict with success status and file info
        """
        import base64

        base_path = _resolve_packs_base()
        if base_path is None:
            return {
                "error": "Reference capability packs not found. "
                         "Set DEVICE_CONNECT_CAPABILITY_PACKS_PATH environment variable."
            }

        pack_path = base_path / reference_pack

        if not pack_path.exists():
            available = [d.name for d in base_path.iterdir() if d.is_dir() and not d.name.startswith(".")]
            return {
                "error": f"Reference pack '{reference_pack}' not found",
                "available_packs": available
            }

        # Resolve file within pack
        local_file = pack_path / file_path
        if not local_file.exists():
            # List available files in pack
            available_files = [str(f.relative_to(pack_path)) for f in pack_path.rglob("*") if f.is_file()]
            return {
                "error": f"File '{file_path}' not found in pack '{reference_pack}'",
                "available_files": available_files[:20]  # Limit to first 20
            }

        # Read file content
        is_binary = False
        try:
            content = local_file.read_text()
        except UnicodeDecodeError:
            # Binary file - read as bytes and base64 encode
            binary_data = local_file.read_bytes()
            content = base64.b64encode(binary_data).decode('ascii')
            is_binary = True
            logger.info(f"Copying binary file: {file_path} ({len(binary_data)} bytes)")

        # Send to device
        command = {
            "tool": "Write",
            "path": remote_path,
            "content": content
        }
        if is_binary:
            command["binary"] = True

        result = await self.send_command(device_id, command)

        if "error" in result:
            return {"error": f"Failed to write {remote_path}: {result['error']}"}

        return {
            "success": True,
            "reference_pack": reference_pack,
            "file_path": file_path,
            "remote_path": remote_path,
            "is_binary": is_binary,
            "size_bytes": local_file.stat().st_size
        }


# --- MCP Protocol Handlers ---

def get_tools_list() -> List[dict]:
    """Get the list of available MCP tools."""
    tools = []

    for func in MCP_TOOLS:
        if hasattr(func, "_mcp_tool"):
            tools.append(func._mcp_tool)

    return tools


async def handle_tool_call(name: str, arguments: dict) -> dict:
    """Handle an MCP tool call."""
    server = DeviceToolsServer()

    # Find the tool function
    for func in MCP_TOOLS:
        if hasattr(func, "_mcp_tool") and func._mcp_tool["name"] == name:
            method = getattr(server, func.__name__)
            try:
                result = await method(**arguments)
                return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}
            except Exception as e:
                return {"content": [{"type": "text", "text": json.dumps({"error": str(e)})}], "isError": True}

    return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}


async def run_stdio_server():
    """Run the MCP server in stdio mode."""
    logger.info("Starting MCP Device Tools server (stdio mode)")

    msg_id = None
    while True:
        try:
            line = await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
            if not line:
                break

            message = json.loads(line.strip())
            method = message.get("method")
            params = message.get("params", {})
            msg_id = message.get("id")

            # JSON-RPC 2.0: notifications have no "id" and MUST NOT receive responses
            if "id" not in message:
                continue

            if method == "initialize":
                response = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {
                            "name": "device-connect-device-tools",
                            "version": "1.0.0"
                        }
                    }
                }
            elif method == "tools/list":
                response = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"tools": get_tools_list()}
                }
            elif method == "tools/call":
                result = await handle_tool_call(params.get("name"), params.get("arguments", {}))
                response = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": result
                }
            else:
                response = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"}
                }

            print(json.dumps(response), flush=True)

        except json.JSONDecodeError:
            continue
        except Exception as e:
            logger.error(f"Error handling message: {e}")
            if msg_id:
                print(json.dumps({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32603, "message": str(e)}
                }), flush=True)


def main():
    """Main entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        stream=sys.stderr  # Log to stderr to not interfere with MCP protocol
    )

    asyncio.run(run_stdio_server())


if __name__ == "__main__":
    main()

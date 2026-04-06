"""Test capability for container integration tests.

A simple echo/math capability that exercises @rpc, @emit, and @periodic
through the sidecar runtime to verify end-to-end container IPC.
"""

from device_connect_edge.drivers.decorators import rpc, emit, periodic


class EchoCapability:
    def __init__(self, device=None):
        self.device = device
        self.call_count = 0

    @rpc()
    async def echo(self, message: str = "hello") -> dict:
        """Echo back the message with metadata."""
        self.call_count += 1
        return {
            "echo": message,
            "call_count": self.call_count,
            "source": "container-sidecar",
        }

    @rpc()
    async def add(self, a: float = 0, b: float = 0) -> dict:
        """Add two numbers."""
        return {"result": a + b}

    @rpc()
    async def get_info(self) -> dict:
        """Return capability runtime info."""
        return {
            "capability_id": "echo-cap",
            "call_count": self.call_count,
            "mode": "sidecar",
        }

    @emit()
    async def heartbeat(self, status: str = "ok"):
        """Periodic heartbeat event."""
        pass

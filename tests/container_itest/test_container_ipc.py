"""Integration tests for container-mode IPC via Zenoh router.

Tests that a capability sidecar running in its own container can:
1. Respond to JSON-RPC commands via the Zenoh router
2. Handle the _describe introspection method
3. Return proper error responses for unknown methods
4. Maintain state across calls (call_count)
5. Publish health messages

Prerequisites:
    docker compose -f tests/container_itest/docker-compose-container-itest.yml up -d --build

Usage:
    DEVICE_CONNECT_ALLOW_INSECURE=true pytest tests/container_itest/ -v
"""

import asyncio
import json
import logging
import os
import subprocess
import time

import pytest
import pytest_asyncio

logger = logging.getLogger(__name__)

ZENOH_ROUTER_URL = os.getenv("CITEST_ZENOH_URL", "tcp/localhost:7448")
DEVICE_ID = "itest-device-001"
TENANT = "default"
CAP_ID = "echo-cap"

# Zenoh topic for the sidecar's command channel
CMD_SUBJECT = f"device-connect.{TENANT}.{DEVICE_ID}.cap.{CAP_ID}.cmd"
HEALTH_SUBJECT = f"device-connect.{TENANT}.{DEVICE_ID}.cap.{CAP_ID}.health"


def _compose_file():
    """Path to the container integration test docker-compose."""
    return str(
        os.path.join(os.path.dirname(__file__), "docker-compose-container-itest.yml")
    )


def _infra_running() -> bool:
    """Check if the container itest infrastructure is up."""
    try:
        result = subprocess.run(
            ["docker", "compose", "-f", _compose_file(), "ps", "--format", "json"],
            capture_output=True,
            timeout=5,
        )
        if result.returncode != 0:
            return False
        output = result.stdout.decode()
        return "citest-zenoh-router" in output and "citest-echo-cap" in output
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# Skip all tests if infrastructure isn't running
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _infra_running(),
        reason=(
            "Container itest infrastructure not running. Start with:\n"
            "  docker compose -f tests/container_itest/docker-compose-container-itest.yml up -d --build"
        ),
    ),
]


@pytest_asyncio.fixture
async def zenoh_client():
    """Connect a Zenoh messaging client to the local router."""
    os.environ["DEVICE_CONNECT_ALLOW_INSECURE"] = "true"
    from device_connect_edge.messaging import create_client

    client = create_client("zenoh")
    await client.connect(servers=[ZENOH_ROUTER_URL])

    # Wait for sidecar to be ready (health messages)
    ready = False
    for _ in range(20):  # 20 * 0.5s = 10s max wait
        await asyncio.sleep(0.5)
        try:
            # Try a ping to see if the sidecar responds
            request = json.dumps({
                "jsonrpc": "2.0",
                "id": "health-check",
                "method": "_describe",
                "params": {},
            }).encode()
            response = await client.request(CMD_SUBJECT, request, timeout=2.0)
            if response:
                ready = True
                break
        except Exception:
            continue

    if not ready:
        logger.warning("Sidecar may not be ready — tests may fail")

    try:
        yield client
    finally:
        await client.close()


# ── RPC through sidecar container ────────────────────────────────


class TestContainerSidecarRpc:
    """Test JSON-RPC calls routed through the Zenoh router to the sidecar container."""

    @pytest.mark.asyncio
    async def test_echo_rpc(self, zenoh_client):
        """Call echo() on the sidecar and verify response."""
        request = {
            "jsonrpc": "2.0",
            "id": "citest-echo-1",
            "method": "echo",
            "params": {"message": "hello from host"},
        }
        response_bytes = await zenoh_client.request(
            CMD_SUBJECT,
            json.dumps(request).encode(),
            timeout=5.0,
        )
        response = json.loads(response_bytes)

        assert "result" in response, f"RPC failed: {response}"
        assert response["result"]["echo"] == "hello from host"
        assert response["result"]["source"] == "container-sidecar"
        assert response["id"] == "citest-echo-1"

    @pytest.mark.asyncio
    async def test_add_rpc(self, zenoh_client):
        """Call add() and verify arithmetic."""
        request = {
            "jsonrpc": "2.0",
            "id": "citest-add-1",
            "method": "add",
            "params": {"a": 17, "b": 25},
        }
        response_bytes = await zenoh_client.request(
            CMD_SUBJECT,
            json.dumps(request).encode(),
            timeout=5.0,
        )
        response = json.loads(response_bytes)

        assert response["result"]["result"] == 42

    @pytest.mark.asyncio
    async def test_state_persists_across_calls(self, zenoh_client):
        """Call echo twice and verify call_count increments."""
        for i in range(3):
            request = {
                "jsonrpc": "2.0",
                "id": f"citest-state-{i}",
                "method": "echo",
                "params": {},
            }
            response_bytes = await zenoh_client.request(
                CMD_SUBJECT,
                json.dumps(request).encode(),
                timeout=5.0,
            )

        response = json.loads(response_bytes)
        # call_count should be > 1 (exact value depends on test ordering)
        assert response["result"]["call_count"] >= 3


# ── _describe introspection ──────────────────────────────────────


class TestContainerSidecarDescribe:
    """Test the _describe built-in method for capability introspection."""

    @pytest.mark.asyncio
    async def test_describe_returns_functions(self, zenoh_client):
        """_describe should list all @rpc functions with schemas."""
        request = {
            "jsonrpc": "2.0",
            "id": "citest-describe-1",
            "method": "_describe",
            "params": {},
        }
        response_bytes = await zenoh_client.request(
            CMD_SUBJECT,
            json.dumps(request).encode(),
            timeout=5.0,
        )
        response = json.loads(response_bytes)

        assert "result" in response
        result = response["result"]
        assert result["capability_id"] == "echo-cap"
        assert "echo" in result["functions"]
        assert "add" in result["functions"]
        assert "get_info" in result["functions"]

    @pytest.mark.asyncio
    async def test_describe_includes_schemas(self, zenoh_client):
        """Each function in _describe should have description and parameters."""
        request = {
            "jsonrpc": "2.0",
            "id": "citest-describe-2",
            "method": "_describe",
            "params": {},
        }
        response_bytes = await zenoh_client.request(
            CMD_SUBJECT,
            json.dumps(request).encode(),
            timeout=5.0,
        )
        response = json.loads(response_bytes)

        echo_schema = response["result"]["functions"]["echo"]
        assert "description" in echo_schema
        assert "parameters" in echo_schema


# ── Error handling ───────────────────────────────────────────────


class TestContainerSidecarErrors:
    """Test error handling in the sidecar."""

    @pytest.mark.asyncio
    async def test_unknown_method_returns_error(self, zenoh_client):
        """Calling a non-existent method should return JSON-RPC error."""
        request = {
            "jsonrpc": "2.0",
            "id": "citest-err-1",
            "method": "nonexistent_method",
            "params": {},
        }
        response_bytes = await zenoh_client.request(
            CMD_SUBJECT,
            json.dumps(request).encode(),
            timeout=5.0,
        )
        response = json.loads(response_bytes)

        assert "error" in response
        assert response["error"]["code"] == -32601
        assert "Method not found" in response["error"]["message"]

"""Docker Compose infrastructure management for cross-repo integration tests.

Manages NATS + Zenoh + etcd + device-registry-service lifecycle.
Dev mode only (no TLS, no JWT auth) — matches DEVICE_CONNECT_ALLOW_INSECURE=true.
"""

import asyncio
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

ITEST_ROOT = Path(__file__).parents[1]
COMPOSE_FILE = ITEST_ROOT / "docker-compose-itest.yml"

DEFAULT_NATS_URL = os.getenv("NATS_URL", "nats://localhost:4222")
DEFAULT_ETCD_URL = os.getenv("ETCD_URL", "http://localhost:2379")
DEFAULT_ZENOH_REST_URL = os.getenv("ZENOH_REST_URL", "http://localhost:8001")


class DockerComposeManager:
    """Manages Docker Compose infrastructure lifecycle for itests."""

    def __init__(self, compose_file: Path = COMPOSE_FILE, services: list[str] | None = None):
        self.compose_file = compose_file
        self.services = services or ["nats", "zenoh", "etcd", "device-registry-service"]
        self._started_by_us = False

    async def is_running(self) -> bool:
        """Check if infrastructure is already running.

        Detects both itest-specific containers *and* any other containers
        occupying the required ports (e.g. from core/docker-compose-dev.yml).
        """
        # First check for our own itest containers
        try:
            result = subprocess.run(
                ["docker", "ps", "-q", "-f", "name=itest-nats"],
                capture_output=True, text=True, timeout=5,
            )
            if result.stdout.strip():
                return True
        except Exception:
            pass

        # Also check if the required ports are already bound by other containers
        for port in ("4222", "2379"):
            try:
                result = subprocess.run(
                    ["docker", "ps", "-q", "-f", f"publish={port}"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.stdout.strip():
                    logger.info(
                        "Port %s already bound by another container — "
                        "reusing existing infrastructure", port,
                    )
                    return True
            except Exception:
                pass

        return False

    async def start(self, force: bool = False) -> bool:
        if not force and await self.is_running():
            logger.info("Infrastructure already running, skipping start")
            return False

        logger.info(f"Starting Docker Compose services: {self.services}")
        cmd = [
            "docker", "compose", "-f", str(self.compose_file),
            "up", "-d", "--wait",
        ] + self.services

        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=ITEST_ROOT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"Failed to start infrastructure: {stderr.decode()}")

        self._started_by_us = True
        logger.info("Docker Compose services started")
        return True

    async def stop(self) -> None:
        if not self._started_by_us:
            return
        logger.info("Stopping Docker Compose services")
        cmd = [
            "docker", "compose", "-f", str(self.compose_file),
            "down", "-v", "--remove-orphans",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=ITEST_ROOT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

    async def logs(self, service: str | None = None, tail: int = 50) -> str:
        cmd = ["docker", "compose", "-f", str(self.compose_file), "logs", "--tail", str(tail)]
        if service:
            cmd.append(service)
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=ITEST_ROOT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return stdout.decode() + stderr.decode()


async def wait_for_nats(url: str = DEFAULT_NATS_URL, timeout: float = 30) -> None:
    """Wait for NATS to be reachable.

    Checks known container names (``itest-nats``, ``nats-jwt``) for Docker
    health status, then falls back to a direct TCP connection check so that
    pre-existing infrastructure from other compose files is detected.
    """
    logger.info(f"Waiting for NATS at {url}")
    import socket

    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < timeout:
        # Try Docker health for known container names
        for name in ("itest-nats", "nats-jwt"):
            try:
                result = subprocess.run(
                    ["docker", "inspect", "--format", "{{.State.Health.Status}}", name],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0 and "healthy" in result.stdout.strip():
                    logger.info(f"NATS is healthy (container: {name})")
                    return
            except Exception:
                pass

        # Fallback: TCP connect to NATS port
        try:
            with socket.create_connection(("localhost", 4222), timeout=2):
                logger.info("NATS is reachable via TCP")
                return
        except OSError:
            pass

        await asyncio.sleep(0.5)
    raise TimeoutError(f"NATS did not become healthy within {timeout}s")


async def wait_for_etcd(url: str = DEFAULT_ETCD_URL, timeout: float = 30) -> None:
    """Wait for etcd health endpoint."""
    import aiohttp
    logger.info(f"Waiting for etcd at {url}/health")
    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < timeout:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{url}/health", timeout=2) as resp:
                    if resp.status == 200:
                        logger.info("etcd is healthy")
                        return
        except Exception:
            pass
        await asyncio.sleep(0.5)
    raise TimeoutError(f"etcd did not become healthy within {timeout}s")


async def wait_for_zenoh(
    rest_url: str = DEFAULT_ZENOH_REST_URL, timeout: float = 30
) -> None:
    """Wait for Zenoh router REST plugin to respond."""
    import socket

    logger.info(f"Waiting for Zenoh at {rest_url}")
    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < timeout:
        # Try Docker health for known container name
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Health.Status}}", "itest-zenoh"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and "healthy" in result.stdout.strip():
                logger.info("Zenoh is healthy (container: itest-zenoh)")
                return
        except Exception:
            pass

        # Fallback: TCP connect to Zenoh router port
        try:
            with socket.create_connection(("localhost", 7447), timeout=2):
                logger.info("Zenoh is reachable via TCP")
                return
        except OSError:
            pass

        await asyncio.sleep(0.5)
    raise TimeoutError(f"Zenoh did not become healthy within {timeout}s")


async def wait_for_all_services(
    nats_url: str = DEFAULT_NATS_URL,
    etcd_url: str = DEFAULT_ETCD_URL,
    zenoh_rest_url: str = DEFAULT_ZENOH_REST_URL,
    timeout: float = 60,
) -> None:
    await wait_for_etcd(etcd_url, timeout=timeout)
    await wait_for_nats(nats_url, timeout=timeout)
    await wait_for_zenoh(zenoh_rest_url, timeout=timeout)
    logger.info("All infrastructure services healthy")


async def clear_device_registry(etcd_host: str = "localhost", etcd_port: int = 2379) -> int:
    """Clear all devices from the registry via etcd HTTP API."""
    import base64
    import aiohttp

    base_url = f"http://{etcd_host}:{etcd_port}"
    prefix = "/device-connect/devices/"
    key_start = base64.b64encode(prefix.encode()).decode()
    prefix_end = prefix[:-1] + chr(ord(prefix[-1]) + 1)
    key_end = base64.b64encode(prefix_end.encode()).decode()

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{base_url}/v3/kv/range",
            json={"key": key_start, "range_end": key_end, "count_only": True},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return 0
            data = await resp.json()
            count = int(data.get("count", 0))

        if count > 0:
            await session.post(
                f"{base_url}/v3/kv/deleterange",
                json={"key": key_start, "range_end": key_end},
                timeout=aiohttp.ClientTimeout(total=10),
            )
            logger.info(f"Cleared {count} devices from registry")
        return count

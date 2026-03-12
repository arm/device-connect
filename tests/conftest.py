"""Root conftest for cross-repo integration tests.

Infrastructure:
    - Session-scoped Docker Compose (NATS + Zenoh + etcd + registry)
    - Dev mode: no TLS, no JWT (DEVICE_CONNECT_ALLOW_INSECURE=true)

Fixtures:
    - device_spawner: DeviceFactory using device_connect_sdk package
    - event_capture: EventCollector for messaging event capture
    - event_injector: EventInjector for simulating device events
    - mock_orchestrator: Rule-based orchestrator (no LLM)
    - messaging_client: Connected SDK MessagingClient for direct RPC calls

Backend parameterization:
    All fixtures are parameterized over NATS and Zenoh via messaging_backend.
    Use --backend=nats or --backend=zenoh to run a single backend.

"""

import logging
import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio

# Ensure drivers/ and fixtures/ are importable
ITEST_ROOT = Path(__file__).parent
if str(ITEST_ROOT) not in sys.path:
    sys.path.insert(0, str(ITEST_ROOT))

from fixtures.infrastructure import (
    DockerComposeManager,
    clear_device_registry,
    wait_for_all_services,
)

logger = logging.getLogger(__name__)

BACKEND_URLS = {
    "nats": os.getenv("NATS_URL", "nats://localhost:4222"),
    "zenoh": os.getenv("ZENOH_CONNECT", "tcp/localhost:7447"),
}


# ── Pytest hooks ──────────────────────────────────────────────────

def pytest_addoption(parser):
    parser.addoption(
        "--backend", action="store", default=None,
        help="Run only this messaging backend (nats or zenoh)",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: requires Docker infrastructure")
    config.addinivalue_line("markers", "llm: requires real LLM API key")
    config.addinivalue_line("markers", "slow: takes > 30 seconds")
    config.addinivalue_line("markers", "conformance: messaging backend conformance test")


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "tests" in str(item.fspath):
            item.add_marker(pytest.mark.integration)


# ── Session-scoped infrastructure ──────────────────────────────────

@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def infrastructure():
    """Start Docker Compose infrastructure for the test session."""
    manager = DockerComposeManager()
    try:
        await manager.start()
        await wait_for_all_services()
        logger.info("Infrastructure ready")
        yield manager
    finally:
        if manager._started_by_us:
            keep = os.getenv("ITEST_KEEP_INFRA", "").lower() in ("1", "true", "yes")
            if not keep:
                await manager.stop()
            else:
                logger.info("Keeping infrastructure running (ITEST_KEEP_INFRA=1)")


# ── Backend parameterization ──────────────────────────────────────

@pytest.fixture(params=["nats", "zenoh"])
def messaging_backend(request):
    """Parameterized messaging backend — tests run once per backend."""
    selected = request.config.getoption("--backend")
    if selected and request.param != selected:
        pytest.skip(f"Skipping {request.param} (--backend={selected})")
    return request.param


@pytest.fixture
def messaging_url(messaging_backend):
    """Messaging broker URL for the current backend."""
    return BACKEND_URLS[messaging_backend]


@pytest.fixture(autouse=True)
def _set_backend_env(messaging_backend):
    """Set env vars so SDK/agent-tools auto-detect the correct backend."""
    os.environ["MESSAGING_BACKEND"] = messaging_backend
    if messaging_backend == "zenoh":
        os.environ["DEVICE_CONNECT_DISCOVERY_MODE"] = "p2p"
        os.environ["ZENOH_CONNECT"] = BACKEND_URLS["zenoh"]
    yield
    os.environ.pop("MESSAGING_BACKEND", None)
    os.environ.pop("DEVICE_CONNECT_DISCOVERY_MODE", None)
    os.environ.pop("ZENOH_CONNECT", None)


# Keep nats_url as alias for backward compatibility
@pytest.fixture
def nats_url(messaging_url) -> str:
    return messaging_url


# ── Messaging client (for direct RPC calls in tests) ─────────────

@pytest_asyncio.fixture
async def messaging_client(infrastructure, messaging_backend, messaging_url):
    """Connected SDK MessagingClient for direct RPC calls in tests."""
    from device_connect_sdk.messaging import create_client

    client = create_client(messaging_backend)
    await client.connect(servers=[messaging_url])
    try:
        yield client
    finally:
        await client.close()


# ── Device spawner (uses device_connect_sdk) ────────────────────────────

@pytest_asyncio.fixture
async def device_spawner(infrastructure, messaging_url):
    """Factory for spawning simulated devices via device_connect_sdk."""
    from fixtures.devices import DeviceFactory

    factory = DeviceFactory(messaging_url=messaging_url)
    try:
        yield factory
    finally:
        await factory.cleanup()


# ── Event capture ──────────────────────────────────────────────────

@pytest_asyncio.fixture
async def event_capture(infrastructure, messaging_backend, messaging_url):
    """Messaging event capture utility."""
    from fixtures.events import EventCollector

    collector = EventCollector(backend=messaging_backend, url=messaging_url)
    async with collector:
        yield collector


# ── Event injector ─────────────────────────────────────────────────

@pytest_asyncio.fixture
async def event_injector(infrastructure, messaging_backend, messaging_url):
    """Messaging event injection utility."""
    from fixtures.inject import EventInjector

    injector = EventInjector(backend=messaging_backend, url=messaging_url)
    async with injector:
        yield injector


# ── Mock orchestrator (no LLM) ────────────────────────────────────

@pytest_asyncio.fixture
async def mock_orchestrator(infrastructure, messaging_backend, messaging_url):
    """Rule-based orchestrator for fast tests (no LLM)."""
    from fixtures.orchestrator import MockOrchestrator

    orchestrator = MockOrchestrator(backend=messaging_backend, url=messaging_url)
    async with orchestrator:
        yield orchestrator


# ── Registry cleanup ──────────────────────────────────────────────

@pytest_asyncio.fixture
async def clear_registry(infrastructure):
    """Clear all devices from registry before test."""
    count = await clear_device_registry()
    logger.info(f"Registry cleared: {count} devices removed")
    yield count

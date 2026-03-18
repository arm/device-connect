"""Shared fixtures for device-connect-agent-tools tests.

Uses device_connect_sdk.messaging.MessagingConfig for credential/TLS resolution.
"""

import os
from pathlib import Path
from typing import Optional

import pytest
import pytest_asyncio

from device_connect_sdk.messaging import create_client
from device_connect_sdk.messaging.config import MessagingConfig


def _connection_kwargs() -> dict:
    """Build device_connect_agent_tools.connect() kwargs from MessagingConfig."""
    cfg = MessagingConfig()
    kwargs: dict = {"nats_url": cfg.servers[0] if cfg.servers else "nats://localhost:4222"}
    if cfg.credentials:
        kwargs["credentials"] = cfg.credentials
    if cfg.tls_config:
        kwargs["tls_config"] = cfg.tls_config
    return kwargs


@pytest_asyncio.fixture
async def nats_client():
    """Provide a connected MessagingClient for tests."""
    cfg = MessagingConfig()
    client = create_client()
    await client.connect(
        servers=cfg.servers,
        credentials=cfg.credentials,
        tls_config=cfg.tls_config,
    )
    try:
        yield client
    finally:
        await client.disconnect()


@pytest.fixture
def device_connect_connection():
    """Connect device_connect_agent_tools, yield kwargs, then disconnect."""
    from device_connect_agent_tools import connect, disconnect

    kwargs = _connection_kwargs()
    connect(**kwargs)
    yield kwargs
    disconnect()


def _get_api_key() -> tuple[Optional[str], Optional[str]]:
    """Get LLM API key from env or secrets file."""
    secrets_dirs = [
        Path(__file__).resolve().parents[1] / "secrets",
        Path(__file__).resolve().parents[2] / "core" / "secrets",
    ]

    for provider, env_var, filename in [
        ("anthropic", "ANTHROPIC_API_KEY", "anthropic_api_key"),
        ("openai", "OPENAI_API_KEY", "openai_api_key"),
    ]:
        key = os.getenv(env_var)
        if key:
            return key.strip(), provider
        for secrets_dir in secrets_dirs:
            f = secrets_dir / filename
            if f.exists():
                key = f.read_text().strip()
                if key:
                    return key, provider
    return None, None


@pytest.fixture
def api_key():
    """Provide an LLM API key, or skip if not available."""
    key, provider = _get_api_key()
    if not key:
        pytest.skip("No LLM API key found (set ANTHROPIC_API_KEY or OPENAI_API_KEY)")
    return key, provider

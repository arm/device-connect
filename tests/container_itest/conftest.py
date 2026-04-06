"""Conftest for container integration tests.

Overrides the root conftest's backend parameterization — container IPC
tests only use Zenoh (the sidecar connects to the Zenoh router).
"""

import os
import pytest


# Override the root conftest's parameterized messaging_backend fixture
# to only use zenoh for container IPC tests.
@pytest.fixture
def messaging_backend():
    return "zenoh"


@pytest.fixture
def messaging_url():
    return os.getenv("CITEST_ZENOH_URL", "tcp/localhost:7448")


@pytest.fixture(autouse=True)
def _set_container_env():
    """Set env vars for container itest."""
    os.environ["DEVICE_CONNECT_ALLOW_INSECURE"] = "true"
    os.environ["MESSAGING_BACKEND"] = "zenoh"
    os.environ["ZENOH_CONNECT"] = os.getenv("CITEST_ZENOH_URL", "tcp/localhost:7448")
    yield
    os.environ.pop("MESSAGING_BACKEND", None)
    os.environ.pop("ZENOH_CONNECT", None)

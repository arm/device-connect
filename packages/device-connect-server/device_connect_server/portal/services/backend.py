"""Messaging backend abstraction — strategy pattern for NATS, Zenoh, and MQTT.

The admin selects a backend during bootstrap. All portal services
(tenant creation, device provisioning, RPC, verification) dispatch
through the active backend implementation.
"""

import json
import logging
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .. import config

# Tenant and device names flow into NATS subjects, Zenoh key expressions,
# MQTT ACL rules, TLS certificate CNs, and file paths.  A strict allowlist
# prevents injection across all those layers.
_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")


def validate_name(value: str, label: str = "name") -> str:
    """Validate a tenant or device name is safe for use in subjects/ACLs/certs/paths.

    Raises ValueError if invalid.
    """
    if not _SAFE_NAME_RE.match(value):
        raise ValueError(
            f"Invalid {label}: must start with alphanumeric, contain only "
            f"alphanumeric/dot/hyphen/underscore, and be 1-64 characters"
        )
    return value

logger = logging.getLogger(__name__)

# etcd key where the backend choice is persisted
_BACKEND_ETCD_KEY = "/device-connect/portal/config/messaging_backend"

# Module-level cached backend instance
_active_backend: "MessagingBackendService | None" = None


class MessagingBackendService(ABC):
    """Abstract interface for messaging backend operations."""

    @abstractmethod
    def backend_name(self) -> str:
        """Return the backend identifier ('nats', 'zenoh', or 'mqtt')."""

    @abstractmethod
    def is_bootstrapped(self) -> bool:
        """Check if the infrastructure has been set up."""

    @abstractmethod
    async def bootstrap(self, host: str, port: str, **kwargs) -> dict:
        """One-time infrastructure bootstrap. Returns summary dict."""

    @abstractmethod
    async def create_tenant(
        self, tenant: str, num_devices: int, host: str, port: str,
    ) -> list[str]:
        """Create a tenant with N device credentials. Returns device names."""

    @abstractmethod
    async def add_device(
        self, tenant: str, device_name: str, host: str, port: str,
    ) -> Path:
        """Add a single device credential. Returns credential path."""

    @abstractmethod
    async def reload_broker(self) -> dict:
        """Reload the broker config. Returns {success, message}."""

    @abstractmethod
    async def rpc_invoke(
        self, tenant: str, device_id: str, function: str,
        params: dict, timeout: float = 5.0,
    ) -> dict:
        """Send a JSON-RPC request to a device. Returns response dict."""

    @abstractmethod
    async def rpc_connect(self) -> Any:
        """Return a connected messaging client for SSE streaming.

        For NATS: returns a nats.Client.
        For Zenoh: returns a ZenohAdapter.
        """

    @abstractmethod
    async def subscribe_events(
        self, client: Any, subject: str, callback,
    ) -> Any:
        """Subscribe to events on the given client. Returns subscription handle."""

    @abstractmethod
    async def unsubscribe_events(self, client: Any, subscription: Any) -> None:
        """Unsubscribe and close the client."""

    @abstractmethod
    async def run_verification(self) -> list[dict]:
        """Run the isolation verification suite. Returns test results."""

    @abstractmethod
    def broker_display_info(self) -> dict:
        """Return display info for the admin dashboard.

        Returns dict with keys: backend, host, port, and any backend-specific info.
        """

    def default_host(self) -> str:
        """Return the default host for this backend."""
        return "localhost"

    def default_port(self) -> str:
        """Return the default port for this backend."""
        return "4222"


def _read_backend_choice() -> dict | None:
    """Read the persisted backend choice from etcd."""
    try:
        import etcd3gw
        client = etcd3gw.Etcd3Client(host=config.ETCD_HOST, port=config.ETCD_PORT)
        values = client.get(_BACKEND_ETCD_KEY)
        if values and values[0]:
            data = values[0]
            if isinstance(data, bytes):
                data = data.decode()
            return json.loads(data)
    except Exception as e:
        logger.debug("Could not read backend choice from etcd: %s", e)
    return None


def _write_backend_choice(backend: str, host: str, port: str) -> None:
    """Persist the backend choice to etcd."""
    try:
        import etcd3gw
        client = etcd3gw.Etcd3Client(host=config.ETCD_HOST, port=config.ETCD_PORT)
        from datetime import datetime, timezone
        data = {
            "backend": backend,
            "host": host,
            "port": port,
            "bootstrapped_at": datetime.now(timezone.utc).isoformat(),
        }
        client.put(_BACKEND_ETCD_KEY, json.dumps(data))
    except Exception as e:
        logger.warning("Could not persist backend choice to etcd: %s", e)


def _create_backend(name: str) -> MessagingBackendService:
    """Instantiate a backend by name."""
    if name == "nats":
        from .nats_backend import NatsBackend
        return NatsBackend()
    elif name == "zenoh":
        from .zenoh_backend import ZenohBackend
        return ZenohBackend()
    elif name == "mqtt":
        from .mqtt_backend import MqttBackend
        return MqttBackend()
    else:
        raise ValueError(f"Unknown messaging backend: {name}")


def get_backend(name: str | None = None) -> MessagingBackendService:
    """Get the active messaging backend.

    If *name* is given (e.g. during bootstrap), create that backend.
    Otherwise, resolve from: env var -> etcd -> default (nats).
    """
    global _active_backend

    if name:
        _active_backend = _create_backend(name)
        return _active_backend

    if _active_backend is not None:
        return _active_backend

    # 1. Environment variable override
    env_backend = config.MESSAGING_BACKEND
    if env_backend:
        _active_backend = _create_backend(env_backend)
        return _active_backend

    # 2. Persisted choice in etcd
    choice = _read_backend_choice()
    if choice:
        _active_backend = _create_backend(choice["backend"])
        return _active_backend

    # 3. Auto-detect from backend-specific files
    mosquitto_conf = config.SECURITY_INFRA_DIR / "mosquitto.conf"
    if mosquitto_conf.exists():
        _active_backend = _create_backend("mqtt")
        return _active_backend

    zenoh_ca = config.SECURITY_INFRA_DIR / "ca.pem"
    if zenoh_ca.exists() and not (config.NSC_HOME / "nkeys").exists():
        _active_backend = _create_backend("zenoh")
        return _active_backend

    # 4. Default: NATS
    _active_backend = _create_backend("nats")
    return _active_backend


def reset_backend() -> None:
    """Clear the cached backend (used after bootstrap to switch)."""
    global _active_backend
    _active_backend = None

"""
Messaging abstraction layer for Device Connect.

Provides a pluggable interface for different pub/sub messaging systems (NATS, MQTT, Zenoh, etc).
"""

from typing import Callable, Dict

from device_connect_edge.messaging.base import MessagingClient, Subscription
from device_connect_edge.messaging.exceptions import (
    MessagingError,
    ConnectionError,
    PublishError,
    SubscribeError,
    RequestTimeoutError,
)

# Plugin registry for backends provided by other packages (e.g. device-connect-server)
_BACKEND_REGISTRY: Dict[str, Callable[[], MessagingClient]] = {}


def register_backend(name: str, factory_fn: Callable[[], MessagingClient]) -> None:
    """
    Register a custom messaging backend factory.

    This allows external packages (e.g., device-connect-server) to add backends
    that DeviceRuntime can discover via create_client().

    Args:
        name: Backend name (e.g., "zenoh")
        factory_fn: Callable that returns a MessagingClient instance

    Example:
        >>> from device_connect_edge.messaging import register_backend
        >>> register_backend("zenoh", lambda: ZenohAdapter())
    """
    _BACKEND_REGISTRY[name.lower()] = factory_fn


def create_client(backend: str = "zenoh") -> MessagingClient:
    """
    Factory function to create a messaging client for the specified backend.

    Args:
        backend: The messaging backend to use ("zenoh", "nats", "mqtt", or any registered backend)

    Returns:
        MessagingClient instance for the specified backend

    Raises:
        ValueError: If backend is not supported

    Example:
        >>> client = create_client("zenoh")
        >>> await client.connect(servers=["tcp/localhost:7447"])
    """
    backend = backend.lower()

    if backend == "nats":
        from device_connect_edge.messaging.nats_adapter import NATSAdapter
        return NATSAdapter()
    elif backend == "zenoh":
        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter
        return ZenohAdapter()
    elif backend == "mqtt":
        from device_connect_edge.messaging.mqtt_adapter import MQTTAdapter
        return MQTTAdapter()
    elif backend in _BACKEND_REGISTRY:
        return _BACKEND_REGISTRY[backend]()
    else:
        registered = ", ".join(_BACKEND_REGISTRY.keys())
        extras = f", {registered}" if registered else ""
        raise ValueError(
            f"Unsupported messaging backend: {backend}. "
            f"Supported backends: nats, zenoh, mqtt{extras}"
        )


__all__ = [
    "MessagingClient",
    "Subscription",
    "create_client",
    "register_backend",
    "MessagingError",
    "ConnectionError",
    "PublishError",
    "SubscribeError",
    "RequestTimeoutError",
]

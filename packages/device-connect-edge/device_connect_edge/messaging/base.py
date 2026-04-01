"""
Abstract base class for messaging clients.

Defines the interface that all messaging backend adapters must implement.
"""

from abc import ABC, abstractmethod
from typing import Callable, Awaitable, Optional, List, Any, Dict


class Subscription(ABC):
    """Abstract subscription handle."""

    @abstractmethod
    async def unsubscribe(self) -> None:
        """Unsubscribe from the topic/subject."""
        pass


class MessagingClient(ABC):
    """
    Abstract messaging client interface for pub/sub systems.

    This interface abstracts the core messaging operations needed by Device Connect
    to support multiple messaging backends (NATS, MQTT, Zenoh, etc.).
    """

    @abstractmethod
    async def connect(
        self,
        servers: List[str],
        credentials: Optional[Dict[str, Any]] = None,
        tls_config: Optional[Dict[str, Any]] = None,
        reconnect_cb: Optional[Callable[[], Awaitable[None]]] = None,
        disconnect_cb: Optional[Callable[[], Awaitable[None]]] = None,
        **kwargs: Any
    ) -> None:
        """
        Establish connection to messaging broker.

        Args:
            servers: List of server URLs (e.g., ["nats://localhost:4222"])
            credentials: Authentication credentials (backend-specific format)
                For NATS: {"jwt": str, "nkey_seed": str, "signature_cb": Callable}
                For MQTT: {"username": str, "password": str}
            tls_config: TLS configuration
                Common: {"ca_file": str, "cert_file": str, "key_file": str}
            reconnect_cb: Callback when reconnected after disconnection
            disconnect_cb: Callback when disconnected
            **kwargs: Additional backend-specific options

        Raises:
            ConnectionError: If connection fails
            AuthenticationError: If authentication fails
        """
        pass

    @abstractmethod
    async def publish(self, subject: str, data: bytes) -> None:
        """
        Publish message to subject/topic.

        Args:
            subject: Subject or topic to publish to (NATS-style dotted notation)
            data: Message payload as bytes

        Raises:
            PublishError: If publish fails
            NotConnectedError: If not connected to broker
        """
        pass

    @abstractmethod
    async def subscribe(
        self,
        subject: str,
        callback: Callable[[bytes, Optional[str]], Awaitable[None]],
        queue: Optional[str] = None,
        subscribe_only: bool = False,
    ) -> Subscription:
        """
        Subscribe to subject/topic with callback.

        Args:
            subject: Subject or topic pattern to subscribe to
                Supports wildcards: * (single token), > (multiple tokens)
                Example: "device-connect.*.event.*" or "device-connect.tenant.>"
            callback: Async function called for each message
                Signature: async def callback(data: bytes, reply_subject: Optional[str])
            queue: Queue group name for load balancing (optional)
            subscribe_only: If True, set up pub/sub delivery only and skip
                request/reply infrastructure (e.g. Zenoh queryable). Use for
                callers that only consume events and never reply.

        Returns:
            Subscription handle that can be used to unsubscribe

        Raises:
            SubscribeError: If subscription fails
            NotConnectedError: If not connected to broker
        """
        pass

    @abstractmethod
    async def request(
        self,
        subject: str,
        data: bytes,
        timeout: float = 5.0
    ) -> bytes:
        """
        Send request and wait for reply (RPC pattern).

        Args:
            subject: Subject to send request to
            data: Request payload as bytes
            timeout: Maximum time to wait for reply in seconds

        Returns:
            Reply payload as bytes

        Raises:
            RequestTimeoutError: If no reply received within timeout
            PublishError: If request fails to send
            NotConnectedError: If not connected to broker
        """
        pass

    @abstractmethod
    async def close(self) -> None:
        """
        Close connection and cleanup resources.

        Should gracefully shutdown all subscriptions and the connection.
        After calling this, the client should not be reused.
        """
        pass

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """
        Check if currently connected to broker.

        Returns:
            True if connected, False otherwise
        """
        pass

    @property
    @abstractmethod
    def is_closed(self) -> bool:
        """
        Check if connection has been closed.

        Returns:
            True if closed, False otherwise
        """
        pass

    # Optional helper methods that adapters may override

    async def subscribe_with_subject(
        self,
        subject: str,
        callback: Callable[[bytes, str, Optional[str]], Awaitable[None]],
        queue: Optional[str] = None,
        subscribe_only: bool = False,
    ) -> Subscription:
        """
        Subscribe to subject/topic with callback that receives the subject.

        This variant of subscribe() passes the matched subject to the callback,
        which is useful when subscribing to wildcard patterns and needing to
        know which specific subject triggered the callback.

        Args:
            subject: Subject or topic pattern to subscribe to
                Supports wildcards: * (single token), > (multiple tokens)
            callback: Async function called for each message
                Signature: async def callback(data: bytes, subject: str, reply: Optional[str])
            queue: Queue group name for load balancing (optional)
            subscribe_only: If True, set up pub/sub delivery only and skip
                request/reply infrastructure (e.g. Zenoh queryable). Use for
                callers that only consume events and never reply.

        Returns:
            Subscription handle that can be used to unsubscribe

        Raises:
            SubscribeError: If subscription fails
            NotConnectedError: If not connected to broker
            NotImplementedError: If backend doesn't support this method
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement subscribe_with_subject(). "
            "Use subscribe() or check if your backend supports this method."
        )

    async def flush(self) -> None:
        """
        Flush pending publishes to the server.

        Optional operation that some backends may support.
        Default implementation does nothing.
        """
        pass

    async def drain(self) -> None:
        """
        Flush any pending messages and drain connection.

        Optional operation that some backends may support.
        Default implementation does nothing.
        """
        pass

    def convert_subject_syntax(self, subject: str) -> str:
        """
        Convert subject from NATS-style to backend-specific format.

        Default implementation returns subject unchanged.
        MQTT adapter overrides to convert dots to slashes.

        Args:
            subject: NATS-style subject (e.g., "device-connect.tenant.device.event")

        Returns:
            Backend-specific topic/subject format
        """
        return subject

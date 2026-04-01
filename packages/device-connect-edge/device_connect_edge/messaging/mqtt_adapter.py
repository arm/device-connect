"""
MQTT implementation of the messaging abstraction layer.
"""

import asyncio
import ssl
import json
import base64
import uuid
import logging
from pathlib import Path
from typing import Callable, Awaitable, Optional, List, Any, Dict
from urllib.parse import urlparse

try:
    from aiomqtt import Client as MQTTClient
except ImportError:
    MQTTClient = None  # type: ignore

from device_connect_edge.messaging.base import MessagingClient, Subscription
from device_connect_edge.messaging.exceptions import (
    ConnectionError,
    PublishError,
    SubscribeError,
    RequestTimeoutError,
    NotConnectedError,
    AuthenticationError,
)


class MQTTSubscriptionWrapper(Subscription):
    """Wrapper for MQTT subscription."""

    def __init__(self, client: "MQTTClient", topic: str):
        self._client = client
        self._topic = topic

    async def unsubscribe(self) -> None:
        """Unsubscribe from the topic."""
        await self._client.unsubscribe(self._topic)


class MQTTAdapter(MessagingClient):
    """
    MQTT implementation of the MessagingClient interface.

    Provides MQTT 5.0 features including:
    - Shared subscriptions for load balancing ($share/group/topic)
    - QoS levels (0, 1, 2)
    - TLS encryption
    - Username/password authentication
    - Manual RPC pattern via reply-to topics
    - Wildcard subscriptions (+ and #)

    Note: MQTT has some differences from NATS:
    - Single broker (use HAProxy for HA)
    - No native request/reply (implemented manually)
    - Topic format uses slashes (auto-converted from dots)
    - No native JWT auth (uses username/password or TLS certs)
    """

    def __init__(self):
        if MQTTClient is None:
            raise ImportError(
                "aiomqtt library required for MQTT support. "
                "Install with: pip install aiomqtt"
            )

        self._client: Optional[MQTTClient] = None
        self._logger = logging.getLogger(__name__)
        self._subscriptions: Dict[str, Callable] = {}
        self._request_futures: Dict[str, asyncio.Future] = {}
        self._message_loop_task: Optional[asyncio.Task] = None
        self._connected = False
        self._closed = False
        self._reconnect_cb: Optional[Callable[[], Awaitable[None]]] = None
        self._disconnect_cb: Optional[Callable[[], Awaitable[None]]] = None
        self._qos = 1  # Default QoS level

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
        Establish connection to MQTT broker.

        Args:
            servers: List of MQTT broker URLs (only first is used, recommend HAProxy for HA)
            credentials: Authentication credentials
                - username: MQTT username
                - password: MQTT password
            tls_config: TLS configuration
                - ca_file: Path to CA certificate
                - cert_file: Path to client certificate (mTLS)
                - key_file: Path to client key (mTLS)
            reconnect_cb: Callback when reconnected
            disconnect_cb: Callback when disconnected
            **kwargs: Additional MQTT-specific options
                - qos: Default QoS level (0, 1, or 2)
                - keepalive: Keepalive interval in seconds
        """
        if len(servers) > 1:
            self._logger.warning(
                f"MQTT adapter only supports single broker. "
                f"Using first server: {servers[0]}. "
                f"For HA, use HAProxy or similar load balancer."
            )

        # Parse broker URL
        parsed = urlparse(servers[0])
        hostname = parsed.hostname or "localhost"
        port = parsed.port or 1883

        # Detect TLS from scheme
        use_tls = parsed.scheme in ("mqtts", "ssl", "tls")
        if tls_config:
            use_tls = True

        # Store callbacks
        self._reconnect_cb = reconnect_cb
        self._disconnect_cb = disconnect_cb

        # Store QoS from kwargs
        self._qos = kwargs.get("qos", 1)

        try:
            connect_params = {
                "hostname": hostname,
                "port": port,
                "keepalive": kwargs.get("keepalive", 60),
            }

            # Add username/password auth
            if credentials:
                username = credentials.get("username")
                password = credentials.get("password")
                if username:
                    connect_params["username"] = username
                if password:
                    connect_params["password"] = password
                self._logger.info(f"MQTT authentication configured (user: {username})")

            # Add TLS if configured
            if use_tls:
                tls_context = self._build_tls_context(tls_config or {})
                connect_params["tls_context"] = tls_context
                self._logger.info("TLS enabled for secure MQTT connection")

            # Create client and connect
            self._client = MQTTClient(**connect_params)
            await self._client.__aenter__()
            self._connected = True
            self._closed = False

            # Start message dispatch loop
            self._message_loop_task = asyncio.create_task(self._message_loop())

            self._logger.info(f"Connected to MQTT broker: {hostname}:{port}")

        except Exception as e:
            self._logger.error(f"Failed to connect to MQTT: {e}")
            if "authentication" in str(e).lower() or "authorization" in str(e).lower():
                raise AuthenticationError(f"Authentication failed: {e}") from e
            raise ConnectionError(f"Failed to connect to MQTT: {e}") from e

    def _build_tls_context(self, tls_config: Dict[str, Any]) -> ssl.SSLContext:
        """Build SSL context from TLS configuration."""
        ca_file = tls_config.get("ca_file")
        cert_file = tls_config.get("cert_file")
        key_file = tls_config.get("key_file")

        tls_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)

        # Load CA certificate
        if ca_file:
            if not Path(ca_file).exists():
                raise FileNotFoundError(f"TLS CA certificate not found: {ca_file}")
            tls_context.load_verify_locations(cafile=ca_file)

        # Load client certificate for mTLS
        if cert_file and key_file:
            if not Path(cert_file).exists():
                raise FileNotFoundError(f"TLS certificate not found: {cert_file}")
            if not Path(key_file).exists():
                raise FileNotFoundError(f"TLS key not found: {key_file}")

            tls_context.load_cert_chain(certfile=cert_file, keyfile=key_file)
            self._logger.info("mTLS enabled with client certificate")

        return tls_context

    async def _message_loop(self) -> None:
        """
        Background task to receive and dispatch messages to subscribers.

        This loop continuously listens for incoming MQTT messages and
        routes them to the appropriate callback handlers.
        """
        try:
            async for message in self._client.messages:
                topic = message.topic.value
                payload = message.payload

                # Check if it's a reply to a request
                if topic.startswith("_reply/"):
                    request_id = topic.split("/")[1]
                    if request_id in self._request_futures:
                        self._request_futures[request_id].set_result(payload)
                    continue

                # Dispatch to subscriber
                # Match against all subscription patterns
                for pattern, callback in self._subscriptions.items():
                    if self._topic_matches(topic, pattern):
                        try:
                            await callback(payload, None)
                        except Exception as e:
                            self._logger.error(f"Error in subscriber callback: {e}")

        except asyncio.CancelledError:
            self._logger.info("Message loop cancelled")
        except Exception as e:
            self._logger.error(f"Message loop error: {e}")
            self._connected = False
            if self._disconnect_cb:
                await self._disconnect_cb()

    def _topic_matches(self, topic: str, pattern: str) -> bool:
        """
        Check if a topic matches a pattern with wildcards.

        MQTT wildcards:
        - + matches single level
        - # matches multiple levels (must be last)

        Args:
            topic: Actual topic (e.g., "device-connect/tenant/device/event")
            pattern: Pattern with wildcards (e.g., "device-connect/+/device/#")

        Returns:
            True if topic matches pattern
        """
        # Remove shared subscription prefix if present
        if pattern.startswith("$share/"):
            pattern = "/".join(pattern.split("/")[2:])

        topic_parts = topic.split("/")
        pattern_parts = pattern.split("/")

        # Handle multi-level wildcard
        if "#" in pattern_parts:
            hash_idx = pattern_parts.index("#")
            if hash_idx != len(pattern_parts) - 1:
                return False  # # must be last
            pattern_parts = pattern_parts[:hash_idx]
            topic_parts = topic_parts[:hash_idx]

        if len(topic_parts) < len(pattern_parts):
            return False

        # Handle single-level wildcard
        for i, pattern_part in enumerate(pattern_parts):
            if pattern_part == "+":
                continue
            if i >= len(topic_parts) or topic_parts[i] != pattern_part:
                return False

        # If no multi-level wildcard, lengths must match
        if "#" not in pattern and len(topic_parts) != len(pattern_parts):
            return False

        return True

    async def publish(self, subject: str, data: bytes) -> None:
        """
        Publish message to MQTT topic.

        Args:
            subject: Subject in NATS-style notation (will be converted to MQTT topic)
            data: Message payload as bytes
        """
        if not self.is_connected:
            raise NotConnectedError("Not connected to MQTT broker")

        # Convert NATS subject to MQTT topic
        topic = self.convert_subject_syntax(subject)

        try:
            await self._client.publish(topic, data, qos=self._qos)
        except Exception as e:
            self._logger.error(f"Failed to publish to {topic}: {e}")
            raise PublishError(f"Failed to publish: {e}") from e

    async def subscribe(
        self,
        subject: str,
        callback: Callable[[bytes, Optional[str]], Awaitable[None]],
        queue: Optional[str] = None,
        subscribe_only: bool = False,
    ) -> Subscription:
        """
        Subscribe to MQTT topic with callback.

        Args:
            subject: Subject pattern (NATS-style, will be converted to MQTT)
                Wildcards: * → +, > → #
            callback: Async callback function(data: bytes, reply_subject: Optional[str])
            queue: Queue group name for load balancing (uses $share/group/topic)

        Returns:
            Subscription handle
        """
        if not self.is_connected:
            raise NotConnectedError("Not connected to MQTT broker")

        # Convert NATS subject to MQTT topic
        topic = self.convert_subject_syntax(subject)

        # Add shared subscription prefix for queue groups
        subscribe_topic = topic
        if queue:
            subscribe_topic = f"$share/{queue}/{topic}"
            self._logger.debug(f"Subscribing to {topic} (shared group: {queue})")
        else:
            self._logger.debug(f"Subscribing to {topic}")

        try:
            await self._client.subscribe(subscribe_topic, qos=self._qos)
            self._subscriptions[subscribe_topic] = callback
            return MQTTSubscriptionWrapper(self._client, subscribe_topic)

        except Exception as e:
            self._logger.error(f"Failed to subscribe to {topic}: {e}")
            raise SubscribeError(f"Failed to subscribe: {e}") from e

    async def request(
        self,
        subject: str,
        data: bytes,
        timeout: float = 5.0
    ) -> bytes:
        """
        Send request and wait for reply (manual RPC pattern for MQTT).

        Implements RPC by:
        1. Creating unique reply topic
        2. Subscribing to reply topic
        3. Publishing request with embedded reply-to metadata
        4. Waiting for response with timeout

        Args:
            subject: Subject to send request to (NATS-style)
            data: Request payload as bytes
            timeout: Maximum time to wait for reply in seconds

        Returns:
            Reply payload as bytes
        """
        if not self.is_connected:
            raise NotConnectedError("Not connected to MQTT broker")

        # Generate unique request ID and reply topic
        request_id = uuid.uuid4().hex
        reply_topic = f"_reply/{request_id}"

        # Subscribe to reply topic
        await self._client.subscribe(reply_topic, qos=self._qos)

        # Create future for reply
        future = asyncio.Future()
        self._request_futures[request_id] = future

        try:
            # Convert NATS subject to MQTT topic
            topic = self.convert_subject_syntax(subject)

            # Wrap payload with reply-to metadata
            request_payload = {
                "data": base64.b64encode(data).decode(),
                "reply_to": reply_topic,
                "request_id": request_id
            }

            # Publish request
            await self._client.publish(
                topic,
                json.dumps(request_payload).encode(),
                qos=self._qos
            )

            # Wait for reply with timeout
            try:
                reply = await asyncio.wait_for(future, timeout=timeout)
                return reply
            except asyncio.TimeoutError as e:
                raise RequestTimeoutError(
                    f"Request to {subject} timed out after {timeout}s"
                ) from e

        finally:
            # Cleanup
            await self._client.unsubscribe(reply_topic)
            if request_id in self._request_futures:
                del self._request_futures[request_id]

    async def close(self) -> None:
        """Close connection to MQTT broker."""
        if self._message_loop_task:
            self._message_loop_task.cancel()
            try:
                await self._message_loop_task
            except asyncio.CancelledError:
                pass

        if self._client:
            await self._client.__aexit__(None, None, None)
            self._logger.info("MQTT connection closed")

        self._connected = False
        self._closed = True

    @property
    def is_connected(self) -> bool:
        """Check if currently connected to MQTT broker."""
        return self._connected

    @property
    def is_closed(self) -> bool:
        """Check if connection has been closed."""
        return self._closed

    def convert_subject_syntax(self, subject: str) -> str:
        """
        Convert NATS-style subject to MQTT topic format.

        Conversions:
        - Dots (.) → Slashes (/)
        - Single wildcard (*) → Plus (+)
        - Multi-level wildcard (>) → Hash (#)

        Args:
            subject: NATS-style subject (e.g., "device-connect.tenant.*.event.>")

        Returns:
            MQTT topic format (e.g., "device-connect/tenant/+/event/#")

        Example:
            >>> adapter.convert_subject_syntax("device-connect.default.*.event.*")
            "device-connect/default/+/event/+"

            >>> adapter.convert_subject_syntax("device-connect.default.>")
            "device-connect/default/#"
        """
        # Replace dots with slashes
        topic = subject.replace(".", "/")

        # Replace NATS wildcards with MQTT wildcards
        topic = topic.replace("/*/", "/+/")  # Middle wildcards
        topic = topic.replace("/*", "/+")     # End wildcards

        # Multi-level wildcard (must be last)
        if topic.endswith("/>"):
            topic = topic[:-2] + "/#"

        return topic

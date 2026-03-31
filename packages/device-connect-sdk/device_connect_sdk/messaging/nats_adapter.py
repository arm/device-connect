"""
NATS implementation of the messaging abstraction layer.
"""

import asyncio
import ssl
import logging
import time
from pathlib import Path
from typing import Callable, Awaitable, Optional, List, Any, Dict

from nats.aio.client import Client as NATS
from nats.aio.subscription import Subscription as NATSSubscription

from device_connect_sdk.messaging.base import MessagingClient, Subscription
from device_connect_sdk.messaging.exceptions import (
    ConnectionError,
    PublishError,
    SubscribeError,
    RequestTimeoutError,
    NotConnectedError,
    AuthenticationError,
)
from device_connect_sdk.telemetry.tracer import get_tracer, SpanKind, StatusCode
from device_connect_sdk.telemetry.metrics import get_metrics


class NATSSubscriptionWrapper(Subscription):
    """Wrapper for NATS subscription."""

    def __init__(self, subscription: NATSSubscription):
        self._subscription = subscription

    async def unsubscribe(self) -> None:
        """Unsubscribe from the subject."""
        await self._subscription.unsubscribe()


class NATSAdapter(MessagingClient):
    """
    NATS implementation of the MessagingClient interface.

    Provides full access to NATS features including:
    - Multi-server clustering with automatic failover
    - JWT authentication with NKey signatures
    - TLS encryption (including mTLS)
    - Wildcard subscriptions (* and >)
    - Queue groups for load balancing
    - Request/reply pattern
    - Connection state callbacks
    """

    def __init__(self):
        self._nc = NATS()
        self._logger = logging.getLogger(__name__)
        self._nkey_seed: Optional[str] = None
        self._reconnecting = False
        self._ever_connected = False  # Track if we've successfully connected at least once
        self._initial_connect_logged = False  # Track if we've shown initial "waiting" message

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
        Establish connection to NATS server(s).

        Args:
            servers: List of NATS server URLs (e.g., ["nats://localhost:4222"])
            credentials: Authentication credentials
                - jwt: JWT token string
                - nkey_seed: NKey seed for signing challenges
                - signature_cb: Optional custom signature callback
            tls_config: TLS configuration
                - ca_file: Path to CA certificate
                - cert_file: Path to client certificate (mTLS)
                - key_file: Path to client key (mTLS)
                - verify_hostname: Whether to verify hostname (default: False for self-signed)
            reconnect_cb: Callback when reconnected
            disconnect_cb: Callback when disconnected
            **kwargs: Additional NATS-specific options (passed directly to NATS client)
        """
        try:
            connect_options = {
                "servers": servers,
                "reconnect_time_wait": kwargs.get("reconnect_time_wait", 2),
                "max_reconnect_attempts": kwargs.get("max_reconnect_attempts", -1),
            }

            # Wrap disconnect callback to track reconnection state
            async def on_disconnect_wrapper():
                self._reconnecting = True
                if disconnect_cb:
                    await disconnect_cb()

            # Wrap reconnect callback to clear reconnection state
            async def on_reconnect_wrapper():
                self._reconnecting = False
                self._ever_connected = True
                if reconnect_cb:
                    await reconnect_cb()

            # Error callback to suppress verbose connection/reconnection tracebacks
            async def on_error(e):
                error_msg = str(e)

                # Check if this is a connection-related error (initial or reconnect)
                is_connection_error = any(pattern in error_msg for pattern in [
                    "Connect call failed",
                    "Connection refused",
                    "Multiple exceptions",
                    "empty response",
                    "unexpected EOF"
                ])

                if is_connection_error:
                    # During initial connection or reconnection, summarize nicely
                    if self._reconnecting:
                        self._logger.debug("Reconnection attempt failed (server unavailable)")
                    elif not self._ever_connected:
                        # Initial connection - show one INFO message, then DEBUG for subsequent
                        if not self._initial_connect_logged:
                            self._logger.info("Waiting for NATS server to become available...")
                            self._initial_connect_logged = True
                        else:
                            self._logger.debug("Connection attempt failed (waiting for server)")
                    else:
                        self._logger.debug("Connection error: %s", error_msg)
                else:
                    # Non-connection errors - log as warning
                    self._logger.warning("NATS error: %s", e)

            connect_options["disconnected_cb"] = on_disconnect_wrapper
            connect_options["reconnected_cb"] = on_reconnect_wrapper
            connect_options["error_cb"] = on_error

            # Add JWT authentication if configured
            if credentials:
                jwt = credentials.get("jwt")
                nkey_seed = credentials.get("nkey_seed")
                signature_cb = credentials.get("signature_cb")

                if jwt and nkey_seed:
                    # Store nkey_seed for signing
                    self._nkey_seed = nkey_seed

                    # Use custom signature callback if provided, otherwise use default
                    if signature_cb:
                        connect_options["signature_cb"] = signature_cb
                    else:
                        connect_options["signature_cb"] = self._sign_nonce

                    connect_options["user_jwt_cb"] = lambda: jwt.encode()
                    self._logger.info("JWT authentication configured")
                elif jwt or nkey_seed:
                    self._logger.warning(
                        "Both JWT and NKey seed required for authentication; "
                        "connecting without auth"
                    )

            # Add TLS if configured
            if tls_config:
                tls_context = self._build_tls_context(tls_config)
                connect_options["tls"] = tls_context
                self._logger.info("TLS enabled for secure connection")

            # Merge any additional kwargs (strip Device Connect-specific options
            # that the underlying nats-py client doesn't understand)
            dc_only_keys = {"allow_insecure"}
            connect_options.update(
                {k: v for k, v in kwargs.items() if k not in dc_only_keys}
            )

            # Connect to NATS
            await self._nc.connect(**connect_options)
            self._ever_connected = True
            self._logger.debug(f"Connected to NATS: {servers}")

        except Exception as e:
            self._logger.error(f"Failed to connect to NATS: {e}")
            if "authorization" in str(e).lower():
                raise AuthenticationError(f"Authentication failed: {e}") from e
            raise ConnectionError(f"Failed to connect to NATS: {e}") from e

    def _build_tls_context(self, tls_config: Dict[str, Any]) -> ssl.SSLContext:
        """Build SSL context from TLS configuration."""
        ca_file = tls_config.get("ca_file")
        cert_file = tls_config.get("cert_file")
        key_file = tls_config.get("key_file")
        verify_hostname = tls_config.get("verify_hostname", False)
        # For development with self-signed certificates
        verify_cert = tls_config.get("verify_cert", True)

        if not ca_file:
            raise ValueError("TLS configuration requires ca_file")

        # Validate CA file exists
        if not Path(ca_file).exists():
            raise FileNotFoundError(f"TLS CA certificate not found: {ca_file}")

        tls_context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
        tls_context.load_verify_locations(cafile=ca_file)

        # Load client certificate if provided (for mTLS)
        if cert_file and key_file:
            if not Path(cert_file).exists():
                raise FileNotFoundError(f"TLS certificate not found: {cert_file}")
            if not Path(key_file).exists():
                raise FileNotFoundError(f"TLS key not found: {key_file}")

            tls_context.load_cert_chain(certfile=cert_file, keyfile=key_file)
            self._logger.info("mTLS enabled with client certificate")

        # Hostname verification (disabled for self-signed certs)
        tls_context.check_hostname = verify_hostname

        # Certificate verification (can be disabled for self-signed certs in development)
        if not verify_cert:
            tls_context.check_hostname = False
            tls_context.verify_mode = ssl.CERT_NONE
            self._logger.warning("TLS certificate verification disabled (development mode)")
        else:
            tls_context.verify_mode = ssl.CERT_REQUIRED

        return tls_context

    def _sign_nonce(self, nonce: bytes) -> bytes:
        """
        Sign a nonce with the NKey seed (for JWT authentication).

        Args:
            nonce: Challenge nonce from NATS server

        Returns:
            Signature as base64-encoded bytes (NATS client will decode to string)
        """
        if not self._nkey_seed:
            raise ValueError("NKey seed not configured")

        try:
            import nkeys
        except ImportError:
            raise ImportError(
                "nkeys library required for JWT authentication. "
                "Install with: pip install nkeys"
            )

        import base64

        seed = nkeys.from_seed(self._nkey_seed.encode())
        # Ensure nonce is bytes
        nonce_bytes = nonce.encode() if isinstance(nonce, str) else nonce
        raw_signature = seed.sign(nonce_bytes)
        # Base64 encode and return as bytes (NATS client expects this format)
        return base64.b64encode(raw_signature)

    async def publish(self, subject: str, data: bytes) -> None:
        """
        Publish message to NATS subject.

        Args:
            subject: NATS subject to publish to
            data: Message payload as bytes
        """
        if not self.is_connected:
            raise NotConnectedError("Not connected to NATS")

        tracer = get_tracer()
        metrics = get_metrics()
        with tracer.start_as_current_span(
            "nats.publish",
            kind=SpanKind.PRODUCER,
            attributes={"messaging.destination": subject},
        ) as span:
            try:
                t0 = time.monotonic()
                await self._nc.publish(subject, data)
                metrics.msg_publish_duration.record(
                    (time.monotonic() - t0) * 1000,
                    {"messaging.destination": subject},
                )
                span.set_status(StatusCode.OK)
            except Exception as e:
                self._logger.error(f"Failed to publish to {subject}: {e}")
                span.record_exception(e)
                span.set_status(StatusCode.ERROR, str(e))
                raise PublishError(f"Failed to publish: {e}") from e

    async def subscribe(
        self,
        subject: str,
        callback: Callable[[bytes, Optional[str]], Awaitable[None]],
        queue: Optional[str] = None,
        subscribe_only: bool = False,
    ) -> Subscription:
        """
        Subscribe to NATS subject with callback.

        Args:
            subject: NATS subject pattern (supports * and >)
            callback: Async callback function(data: bytes, reply_subject: Optional[str])
            queue: Queue group name for load balancing (optional)

        Returns:
            Subscription handle
        """
        if not self.is_connected:
            raise NotConnectedError("Not connected to NATS")

        try:
            # Wrap callback to match NATS signature
            async def wrapper(msg):
                await callback(msg.data, msg.reply)

            # Subscribe with or without queue group
            if queue:
                sub = await self._nc.subscribe(subject, queue=queue, cb=wrapper)
                self._logger.debug(f"Subscribed to {subject} (queue: {queue})")
            else:
                sub = await self._nc.subscribe(subject, cb=wrapper)
                self._logger.debug(f"Subscribed to {subject}")

            return NATSSubscriptionWrapper(sub)

        except Exception as e:
            self._logger.error(f"Failed to subscribe to {subject}: {e}")
            raise SubscribeError(f"Failed to subscribe: {e}") from e

    async def subscribe_with_subject(
        self,
        subject: str,
        callback: Callable[[bytes, str, Optional[str]], Awaitable[None]],
        queue: Optional[str] = None,
        subscribe_only: bool = False,
    ) -> Subscription:
        """
        Subscribe to NATS subject with callback that receives the subject.

        This variant passes the matched subject to the callback, useful for
        wildcard subscriptions where you need to know the specific subject.

        Args:
            subject: NATS subject pattern (supports * and >)
            callback: Async callback function(data: bytes, subject: str, reply: Optional[str])
            queue: Queue group name for load balancing (optional)

        Returns:
            Subscription handle
        """
        if not self.is_connected:
            raise NotConnectedError("Not connected to NATS")

        try:
            # Wrap callback to match NATS signature and include subject
            async def wrapper(msg):
                await callback(msg.data, msg.subject, msg.reply)

            # Subscribe with or without queue group
            if queue:
                sub = await self._nc.subscribe(subject, queue=queue, cb=wrapper)
                self._logger.debug(f"Subscribed to {subject} with subject callback (queue: {queue})")
            else:
                sub = await self._nc.subscribe(subject, cb=wrapper)
                self._logger.debug(f"Subscribed to {subject} with subject callback")

            return NATSSubscriptionWrapper(sub)

        except Exception as e:
            self._logger.error(f"Failed to subscribe to {subject}: {e}")
            raise SubscribeError(f"Failed to subscribe: {e}") from e

    async def request(
        self,
        subject: str,
        data: bytes,
        timeout: float = 5.0
    ) -> bytes:
        """
        Send request and wait for reply (RPC pattern).

        Args:
            subject: NATS subject to send request to
            data: Request payload as bytes
            timeout: Maximum time to wait for reply in seconds

        Returns:
            Reply payload as bytes
        """
        if not self.is_connected:
            raise NotConnectedError("Not connected to NATS")

        tracer = get_tracer()
        metrics = get_metrics()
        with tracer.start_as_current_span(
            "nats.request",
            kind=SpanKind.CLIENT,
            attributes={
                "messaging.destination": subject,
                "messaging.timeout_ms": timeout * 1000,
            },
        ) as span:
            try:
                t0 = time.monotonic()
                msg = await self._nc.request(subject, data, timeout=timeout)
                metrics.msg_request_duration.record(
                    (time.monotonic() - t0) * 1000,
                    {"messaging.destination": subject},
                )
                span.set_status(StatusCode.OK)
                return msg.data
            except asyncio.TimeoutError as e:
                span.set_status(StatusCode.ERROR, "timeout")
                raise RequestTimeoutError(
                    f"Request to {subject} timed out after {timeout}s"
                ) from e
            except Exception as e:
                error_str = str(e)
                # "No responders" is expected during recovery when services aren't ready yet
                if "no responders" in error_str.lower():
                    self._logger.debug(f"Request to {subject} failed: {e}")
                else:
                    self._logger.error(f"Request to {subject} failed: {e}")
                span.record_exception(e)
                span.set_status(StatusCode.ERROR, str(e))
                raise PublishError(f"Request failed: {e}") from e

    async def close(self) -> None:
        """Close connection to NATS."""
        if self._nc and not self._nc.is_closed:
            await self._nc.close()
            self._logger.info("NATS connection closed")

    async def disconnect(self) -> None:
        """Alias for close() — disconnect from NATS."""
        await self.close()

    async def flush(self) -> None:
        """Flush pending publishes to the server."""
        if self._nc and not self._nc.is_closed:
            await self._nc.flush()

    async def drain(self) -> None:
        """Flush pending messages and drain connection."""
        if self._nc and not self._nc.is_closed:
            await self._nc.drain()
            self._logger.info("NATS connection drained")

    @property
    def is_connected(self) -> bool:
        """Check if currently connected to NATS."""
        return self._nc.is_connected if self._nc else False

    @property
    def is_closed(self) -> bool:
        """Check if connection has been closed."""
        return self._nc.is_closed if self._nc else True

    def convert_subject_syntax(self, subject: str) -> str:
        """
        NATS uses dotted notation natively, so no conversion needed.

        Args:
            subject: NATS-style subject (e.g., "device-connect.tenant.device.event")

        Returns:
            Same subject unchanged
        """
        return subject

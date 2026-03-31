"""Helper for creating Device Connect devices.

Provides resilient messaging connectivity, automatic device registration with retries,
heartbeat emission, and JSON-RPC command handling. Events are queued while offline
and re-sent once connectivity returns.

Device logic is encapsulated in a DeviceDriver subclass with @rpc
and @emit decorators.

Messaging Backends:
    - Zenoh: D2D mesh with mTLS, multicast scouting, streaming
    - NATS: Enterprise pub/sub with JWT auth, multi-server clustering, native RPC
    - MQTT: IoT-focused with QoS levels, shared subscriptions, TLS

Authentication Modes:
    1. mTLS with client certificates (Zenoh — Recommended for Production):
       - Mutual TLS with per-device client certs signed by a shared CA
       - Generate certs: ./security_infra/generate_tls_certs.sh --client <device>
       - Set MESSAGING_TLS_CA_FILE, MESSAGING_TLS_CERT_FILE, MESSAGING_TLS_KEY_FILE

    2. JWT with NKeys (NATS):
       - Cryptographic device identity using Ed25519 keypairs
       - Auto-discovered by NATS via JWT resolver
       - Use provision_device.py + commissioning flow

    3. Username/Password (MQTT):
       - Standard MQTT authentication
       - Configure via credentials parameter or env vars

    4. Unsecured (Development Only):
       - No authentication required
       - Not suitable for production deployments

Example:
    from fabric import DeviceRuntime
    from device_connect_sdk.drivers import DeviceDriver, rpc

    class CameraDriver(DeviceDriver):
        device_type = "camera"

        @rpc()
        async def capture_image(self, resolution: str = "1080p") -> dict:
            '''Capture an image from the camera.'''
            return {"image_b64": "..."}

    device = DeviceRuntime(
        driver=CameraDriver(),
        device_id="camera-001",
        messaging_urls=["zenoh+tls://localhost:7447"]
    )
    await device.run()
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, List, Tuple, Union, TYPE_CHECKING


# Import messaging abstraction layer
from device_connect_sdk.messaging import MessagingClient, create_client

# Import type models
from device_connect_sdk.types import (
    DeviceCapabilities,
    DeviceIdentity,
    DeviceStatus,
)

# Type checking imports for driver support
if TYPE_CHECKING:
    from device_connect_sdk.drivers.base import DeviceDriver

# Import telemetry for OTel context extraction and auto-init
from device_connect_sdk.telemetry import DeviceConnectTelemetry, get_tracer
from device_connect_sdk.telemetry.propagation import extract_from_meta
from device_connect_sdk.telemetry.tracer import SpanKind

logger = logging.getLogger(__name__)



def build_rpc_response(id_: str, result: Any) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": id_, "result": result}).encode()


class _D2DRouter:
    """Minimal JSON-RPC router for device-to-device invocation.

    Provides the same interface as the device_connect_server orchestration router
    but without telemetry, retries, or the orchestration dependency.
    Devices can call each other using only the SDK.
    """

    def __init__(
        self,
        messaging: MessagingClient,
        tenant: str = "default",
        timeout: float = 30.0,
    ):
        self._messaging = messaging
        self._tenant = tenant
        self._timeout = timeout

    async def invoke(
        self,
        device_id: str,
        function_name: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Send a JSON-RPC request to a device and return the response."""
        timeout = timeout or self._timeout
        params = params or {}

        req_id = f"d2d-{uuid.uuid4().hex[:12]}"
        subject = f"device-connect.{self._tenant}.{device_id}.cmd"

        rpc_payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": function_name,
            "params": params,
        }

        response_data = await self._messaging.request(
            subject, json.dumps(rpc_payload).encode(), timeout=timeout,
        )
        return json.loads(response_data.decode())

    async def publish_event(
        self, device_id: str, event_name: str, params: Dict[str, Any],
    ) -> None:
        """Publish an event on behalf of a device."""
        clean_name = event_name.split("/", 1)[-1] if "/" in event_name else event_name
        subject = f"device-connect.{self._tenant}.{device_id}.event.{clean_name}"
        payload = {"jsonrpc": "2.0", "method": event_name, "params": params}
        await self._messaging.publish(subject, json.dumps(payload).encode())

    async def notify_device(
        self, device_id: str, subject_suffix: str, payload: Dict[str, Any],
    ) -> None:
        """Send a notification to a device-specific subject."""
        subject = f"device-connect.{self._tenant}.{device_id}.{subject_suffix}"
        await self._messaging.publish(subject, json.dumps(payload).encode())


def build_rpc_error(id_: str, code: int, msg: str) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": msg}}
    ).encode()


class DeviceRuntime:
    """High-level runtime for Device Connect devices.

    The DeviceRuntime hosts a DeviceDriver and provides all infrastructure:
    messaging connectivity (NATS/MQTT), device registration, heartbeats,
    command handling, and event emission with automatic retry and offline queueing.

    Args:
        driver: DeviceDriver instance that defines device functions and events

        capabilities: DeviceCapabilities model (functions and events the device exposes)
        identity: DeviceIdentity model (immutable: manufacturer, model, serial, arch)
        status: DeviceStatus model (runtime: location, busy_score, availability)

        device_id: Unique device identifier

        messaging_backend: Messaging backend ("nats" or "mqtt", default: auto-detect)
        messaging_urls: List of broker URLs (e.g., ["nats://srv1:4222", "mqtt://broker:1883"])
        messaging_auth: Authentication credentials (backend-specific dict)
        messaging_tls: TLS configuration (dict with ca_file, cert_file, key_file)
        nats_credentials_file: Path to credentials JSON file

        tenant: Tenant namespace (default: "default")
        ttl: Device registration TTL in seconds (default: 15)
        heartbeat_interval: Heartbeat interval in seconds (default: ttl/3)
        factory_identity_file: Path to factory identity JSON (enables commissioning)
        auto_commission: Auto-enter commissioning mode if not commissioned (default: True)
        commissioning_port: TCP port for commissioning server (default: 5540)

    Example (Driver-based):
        from fabric import DeviceRuntime
        from device_connect_sdk.drivers import DeviceDriver, rpc

        class CameraDriver(DeviceDriver):
            device_type = "camera"

            @rpc()
            async def capture_image(self, resolution: str = "1080p") -> dict:
                return {"image_b64": "..."}

        device = DeviceRuntime(
            driver=CameraDriver(),
            device_id="camera-001",
            messaging_urls=["zenoh+tls://localhost:7447"]
        )

    Example (Type models):
        from fabric import DeviceRuntime
        from device_connect_sdk.types import DeviceCapabilities, DeviceIdentity, DeviceStatus

        device = DeviceRuntime(
            capabilities=DeviceCapabilities(
                description="Temperature sensor",
                functions=[...],
                events=[...]
            ),
            identity=DeviceIdentity(
                device_type="sensor",
                manufacturer="Acme Corp",
                model="TempSensor-X1",
                arch="arm64"
            ),
            status=DeviceStatus(
                location="warehouse-A",
                availability="idle"
            ),
            device_id="sensor-001",
            messaging_urls=["zenoh+tls://localhost:7447"]
        )
    """

    def __init__(
        self,
        *,
        driver: Optional["DeviceDriver"] = None,
        capabilities: Optional[Union[DeviceCapabilities, dict]] = None,
        identity: Optional[Union[DeviceIdentity, dict]] = None,
        status: Optional[Union[DeviceStatus, dict]] = None,
        device_id: Optional[str] = None,

        # Messaging parameters
        messaging_backend: Optional[str] = None,
        messaging_urls: Optional[List[str]] = None,
        messaging_auth: Optional[Dict[str, Any]] = None,
        messaging_tls: Optional[Dict[str, Any]] = None,
        credentials_file: Optional[str] = None,
        nats_credentials_file: Optional[str] = None,  # deprecated alias for credentials_file

        # Common parameters
        tenant: str = "default",
        ttl: int = 15,
        heartbeat_interval: Optional[float] = None,
        factory_identity_file: Optional[str] = None,
        auto_commission: bool = True,
        commissioning_port: int = 5540,
        allow_insecure: Optional[bool] = None,
    ) -> None:
        # Store driver reference and connect driver to this device
        self._driver = driver
        if driver is not None:
            driver.set_device(self)

        # Initialize identity and status payloads
        identity_payload: dict[str, Any] = {}
        status_payload: dict[str, Any] = {}

        # Build capabilities from driver or capabilities parameter
        if driver is not None:
            # Use driver's capabilities
            caps_obj = driver.capabilities

            # Use driver's identity
            driver_identity = driver.identity
            identity_payload = {
                "device_type": driver_identity.device_type,
                "manufacturer": driver_identity.manufacturer,
                "model": driver_identity.model,
                "serial_number": driver_identity.serial_number,
                "firmware_version": driver_identity.firmware_version,
                "arch": driver_identity.arch,
                "description": driver_identity.description,
            }
            # Filter out None values
            identity_payload = {k: v for k, v in identity_payload.items() if v is not None}

            # Use driver's status
            driver_status = driver.status
            status_payload = driver_status.model_dump(exclude_none=True)
            # Convert datetime to ISO string for JSON serialization
            if "ts" in status_payload and hasattr(status_payload["ts"], "isoformat"):
                status_payload["ts"] = status_payload["ts"].isoformat()
        elif capabilities is not None:
            if isinstance(capabilities, DeviceCapabilities):
                caps_obj = capabilities
            elif isinstance(capabilities, dict):
                caps_obj = DeviceCapabilities(**capabilities)
            else:
                raise TypeError("capabilities must be a DeviceCapabilities or dict")
        else:
            caps_obj = DeviceCapabilities(description="", functions=[], events=[])

        # Merge with explicit identity (overrides driver's identity fields)
        if identity is not None:
            if isinstance(identity, DeviceIdentity):
                identity_dict = identity.model_dump(exclude_none=True)
            elif isinstance(identity, dict):
                identity_dict = dict(identity)
            else:
                raise TypeError("identity must be a DeviceIdentity or dict")
            identity_payload.update(identity_dict)

        # Merge with explicit status (overrides driver's status fields)
        if status is not None:
            if isinstance(status, DeviceStatus):
                status_dict = status.model_dump(exclude_none=True)
                # Convert datetime to ISO string for JSON serialization
                if "ts" in status_dict and hasattr(status_dict["ts"], "isoformat"):
                    status_dict["ts"] = status_dict["ts"].isoformat()
            elif isinstance(status, dict):
                status_dict = dict(status)
            else:
                raise TypeError("status must be a DeviceStatus or dict")
            status_payload.update(status_dict)

        self.capabilities = caps_obj
        self.identity = identity_payload
        self.status = status_payload
        self.device_id = device_id or f"device-{uuid.uuid4().hex[:8]}"
        import re
        if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9._-]{0,254}$', self.device_id):
            raise ValueError(
                f"Invalid device_id {self.device_id!r}. Must match "
                f"'^[a-zA-Z0-9][a-zA-Z0-9._-]{{0,254}}$'."
            )
        self.tenant = tenant
        self.ttl = ttl

        # Commissioning support
        self.factory_identity_file = factory_identity_file
        self.auto_commission = auto_commission
        self.commissioning_port = commissioning_port
        # Allow insecure mode (no authentication) - check env var if not explicitly set
        if allow_insecure is None:
            self.allow_insecure = os.getenv("DEVICE_CONNECT_ALLOW_INSECURE", "").lower() in ("1", "true", "yes")
        else:
            self.allow_insecure = allow_insecure
        self._factory_identity: Optional[dict] = None

        # Initialize logger and internal state early (before commissioning checks)
        self.messaging: Optional[MessagingClient] = None  # Will be initialized based on backend
        self._messaging_backend: Optional[str] = messaging_backend or os.getenv("MESSAGING_BACKEND", "").lower() or None
        self._heartbeat_provider: Optional[Callable[[], dict]] = None
        self._event_queue: asyncio.Queue[Tuple[str, bytes]] = asyncio.Queue(maxsize=10000)
        self._connection_callbacks: List[Callable[[bool], Awaitable[None]]] = []
        self._registration_callbacks: List[Callable[[], Awaitable[None]]] = []
        self._logger = logging.getLogger(f"{__name__}.{self.device_id}")

        # Configure logger to output to console if no handlers are set
        if not self._logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            handler.setFormatter(formatter)
            self._logger.addHandler(handler)
            # Set logger level from environment variable, default to INFO
            log_level = os.getenv('DEVICE_CONNECT_LOG_LEVEL', 'INFO').upper()
            self._logger.setLevel(getattr(logging, log_level, logging.INFO))
            # Prevent propagation to root logger to avoid duplicate logs
            self._logger.propagate = False

        # Handle factory identity and commissioning
        if factory_identity_file:
            self._factory_identity = self._load_factory_identity(factory_identity_file)

            # Override device_id from factory identity if not explicitly provided
            if not device_id:
                self.device_id = self._factory_identity['device_id']
                # Update logger with correct device_id
                self._logger = logging.getLogger(f"{__name__}.{self.device_id}")
                if not self._logger.handlers:
                    handler = logging.StreamHandler()
                    formatter = logging.Formatter(
                        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
                    )
                    handler.setFormatter(formatter)
                    self._logger.addHandler(handler)
                    # Set logger level from environment variable, default to INFO
                    log_level = os.getenv('DEVICE_CONNECT_LOG_LEVEL', 'INFO').upper()
                    self._logger.setLevel(getattr(logging, log_level, logging.INFO))
                    # Prevent propagation to root logger to avoid duplicate logs
                    self._logger.propagate = False

            # Determine credentials file path
            inferred_creds_file = self._get_credentials_path_from_identity()

            # If device is commissioned and credentials exist, load them
            if self._is_commissioned() and Path(inferred_creds_file).exists():
                # Override credentials file if not explicitly provided
                if not credentials_file:
                    credentials_file = inferred_creds_file
                self._logger.debug(f"Device commissioned, loading credentials from {credentials_file}")

        # Merge deprecated alias
        credentials_file = credentials_file or nats_credentials_file

        # Auto-read credentials file from env var if not explicitly provided
        if not credentials_file:
            credentials_file = os.getenv("NATS_CREDENTIALS_FILE")

        # Load credentials from file if provided
        creds_urls = None
        creds_device_id = None
        creds_jwt = None
        creds_nkey_seed = None
        creds_tls_config: dict = {}
        if credentials_file:
            creds = self._load_credentials(credentials_file, messaging_urls)
            # Try backend-specific key, fall back to "nats" for backward compat
            backend_key = self._messaging_backend or "zenoh"
            backend_creds = creds.get(backend_key, creds.get("nats", {}))
            creds_urls = backend_creds.get("urls")
            creds_device_id = creds.get("device_id")
            creds_jwt = backend_creds.get("jwt")
            creds_nkey_seed = backend_creds.get("nkey_seed")
            creds_tls_config = backend_creds.get("tls", {})

            # Override device_id from credentials file if not explicitly provided
            # and not from factory_identity
            if creds_device_id and not device_id and not factory_identity_file:
                self.device_id = creds_device_id
                # Update logger with correct device_id
                self._logger = logging.getLogger(f"{__name__}.{self.device_id}")
                if not self._logger.handlers:
                    handler = logging.StreamHandler()
                    formatter = logging.Formatter(
                        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
                    )
                    handler.setFormatter(formatter)
                    self._logger.addHandler(handler)
                    log_level = os.getenv('DEVICE_CONNECT_LOG_LEVEL', 'INFO').upper()
                    self._logger.setLevel(getattr(logging, log_level, logging.INFO))
                    self._logger.propagate = False
                self._logger.info(f"Using device_id from credentials file: {self.device_id}")

        # ===== Messaging Configuration =====

        # D2D mode: no infrastructure needed (Zenoh multicast scouting)
        # Auto-detected when no broker URLs are available + backend is zenoh,
        # or forced via DEVICE_CONNECT_DISCOVERY_MODE=d2d
        self._d2d_mode = os.getenv("DEVICE_CONNECT_DISCOVERY_MODE", "").lower() in ("d2d", "p2p")
        self._d2d_collector = None
        self._d2d_announcer = None

        # Determine broker URLs
        if messaging_urls:
            self.messaging_urls = messaging_urls
        elif os.getenv("MESSAGING_URLS"):
            self.messaging_urls = [url.strip() for url in os.getenv("MESSAGING_URLS").split(",")]
        elif os.getenv("NATS_URL"):
            self.messaging_urls = [os.getenv("NATS_URL")]
        elif os.getenv("ZENOH_CONNECT"):
            self.messaging_urls = [url.strip() for url in os.getenv("ZENOH_CONNECT").split(",")]
            self._messaging_backend = "zenoh"
        elif creds_urls:
            self.messaging_urls = creds_urls
        else:
            # Default: D2D mode with Zenoh multicast scouting (no broker needed)
            self.messaging_urls = []
            self._messaging_backend = self._messaging_backend or "zenoh"
            self._d2d_mode = True

        # Auto-detect backend from URLs if not explicitly specified
        if not self._messaging_backend:
            first_url = self.messaging_urls[0].lower()
            if first_url.startswith("nats://") or first_url.startswith("tls://"):
                self._messaging_backend = "nats"
            elif first_url.startswith("mqtt://") or first_url.startswith("mqtts://"):
                self._messaging_backend = "mqtt"
            elif first_url.startswith("zenoh://") or first_url.startswith("tcp/") or first_url.startswith("tls/"):
                self._messaging_backend = "zenoh"
            else:
                self._messaging_backend = "zenoh"
                self._logger.info(
                    f"Could not auto-detect backend from URL {first_url}, defaulting to Zenoh"
                )

        # Build authentication credentials
        if messaging_auth:
            self.messaging_auth = messaging_auth
        else:
            auth_dict = {}
            jwt = creds_jwt or os.getenv("NATS_JWT")
            nkey_seed = creds_nkey_seed or os.getenv("NATS_NKEY_SEED")
            if jwt:
                auth_dict["jwt"] = jwt
            if nkey_seed:
                auth_dict["nkey_seed"] = nkey_seed
                auth_dict["signature_cb"] = self._sign_nonce

            self.messaging_auth = auth_dict if auth_dict else None

        # Build TLS configuration
        # Env vars take precedence over credentials file for easy overrides
        if messaging_tls:
            self.messaging_tls = messaging_tls
        else:
            tls_dict = {}
            ca = os.getenv("MESSAGING_TLS_CA_FILE") or os.getenv("NATS_TLS_CA_FILE") or creds_tls_config.get("ca_file")
            cert = os.getenv("MESSAGING_TLS_CERT_FILE") or os.getenv("NATS_TLS_CERT_FILE") or creds_tls_config.get("cert_file")
            key = os.getenv("MESSAGING_TLS_KEY_FILE") or os.getenv("NATS_TLS_KEY_FILE") or creds_tls_config.get("key_file")

            if ca:
                tls_dict["ca_file"] = ca
            if cert:
                tls_dict["cert_file"] = cert
            if key:
                tls_dict["key_file"] = key

            self.messaging_tls = tls_dict if tls_dict else None

        # Store commonly accessed attributes
        self.nats_jwt = self.messaging_auth.get("jwt") if self.messaging_auth else None
        self.nats_nkey_seed = self.messaging_auth.get("nkey_seed") if self.messaging_auth else None
        self.tls_ca_file = self.messaging_tls.get("ca_file") if self.messaging_tls else None

        # Internal state
        self._registration_id: Optional[str] = None
        self._registration_expires_at: float = 0.0
        self._heartbeat_interval: float = heartbeat_interval or max(1.0, self.ttl / 3)
        self._registration_lock: Optional[asyncio.Lock] = None  # Initialized in run()

        # Messaging client (initialized in run())
        self.messaging: Optional[MessagingClient] = None

        # Run state (for stop())
        self._run_future: Optional[asyncio.Future] = None
        self._background_tasks: List[asyncio.Task] = []

    def _validate_device_id_from_creds(self, creds: dict) -> None:
        """
        Validate that device_id matches the credentials file.

        Raises helpful error if mismatch detected.
        """
        creds_device_id = creds.get("device_id")
        if creds_device_id and creds_device_id != self.device_id:
            raise ValueError(
                f"\n{'='*70}\n"
                f"❌ DEVICE_ID MISMATCH ERROR\n"
                f"{'='*70}\n"
                f"The DEVICE_ID environment variable does not match the credentials file!\n\n"
                f"  Expected (from credentials): {creds_device_id}\n"
                f"  Got (from DEVICE_ID):        {self.device_id}\n\n"
                f"This causes 'permissions violation' errors because the JWT is issued\n"
                f"for '{creds_device_id}' but the device is trying to publish/subscribe\n"
                f"as '{self.device_id}'.\n\n"
                f"To fix this, set the correct DEVICE_ID:\n"
                f"  export DEVICE_ID={creds_device_id}\n"
                f"{'='*70}\n"
            )

    def _load_credentials(self, credentials_file: str, messaging_urls: Optional[list[str]] = None) -> dict:
        """
        Load credentials from JSON or .creds file.

        Supports both formats:
        1. Enhanced JSON format (.creds.json) - includes URLs, TLS config, device_id
        2. Standard .creds format (nsc generated) - JWT + NKey only

        Args:
            credentials_file: Path to credentials file
            messaging_urls: List of messaging URLs (fallback for .creds format)
        """
        creds_path = Path(credentials_file)

        # Auto-detect enhanced format (.creds.json)
        if credentials_file.endswith('.creds') and not credentials_file.endswith('.creds.json'):
            json_path = Path(str(creds_path).replace('.creds', '.creds.json'))
            if json_path.exists():
                self._logger.info(f"Found enhanced credentials format: {json_path}")
                creds_path = json_path

        if not creds_path.exists():
            raise FileNotFoundError(
                f"\n{'='*70}\n"
                f"❌ CREDENTIALS FILE NOT FOUND\n"
                f"{'='*70}\n"
                f"Could not find credentials file: {credentials_file}\n\n"
                f"Make sure you have:\n"
                f"  1. Set the credentials file path correctly\n"
                f"  2. Verified the file exists and is readable\n\n"
                f"For NATS JWT auth, commission the device first:\n"
                f"  python -m fabric.devctl commission <device-id> --pin <pin>\n"
                f"For Zenoh mTLS, generate a client cert:\n"
                f"  ./security_infra/generate_tls_certs.sh --client <device-id>\n"
                f"{'='*70}\n"
            )

        with open(creds_path) as f:
            content = f.read()

        # Try to parse as JSON first
        try:
            creds = json.loads(content)
            # Validate device_id matches (skip if allow_insecure)
            if not self.allow_insecure:
                self._validate_device_id_from_creds(creds)
            return creds
        except json.JSONDecodeError:
            pass

        # Parse as .creds format
        lines = content.strip().split('\n')
        in_jwt = False
        in_seed = False
        jwt_lines = []
        seed_lines = []

        for line in lines:
            line = line.strip()
            if '-----BEGIN NATS USER JWT-----' in line:
                in_jwt = True
                continue
            elif '------END NATS USER JWT------' in line:
                in_jwt = False
                continue
            elif '-----BEGIN USER NKEY SEED-----' in line:
                in_seed = True
                continue
            elif '------END USER NKEY SEED------' in line:
                in_seed = False
                continue

            if in_jwt and line and not line.startswith('*'):
                jwt_lines.append(line)
            elif in_seed and line and not line.startswith('*'):
                seed_lines.append(line)

        if not jwt_lines or not seed_lines:
            raise ValueError(f"Invalid .creds file format: {creds_path}")

        jwt = ''.join(jwt_lines)
        seed = ''.join(seed_lines)

        # Determine URLs from env var or parameters
        urls = []
        if os.getenv("MESSAGING_URLS"):
            urls = [url.strip() for url in os.getenv("MESSAGING_URLS").split(",") if url.strip()]
        elif os.getenv("NATS_URL"):
            urls = [os.getenv("NATS_URL")]
        elif messaging_urls:
            urls = messaging_urls
        else:
            urls = ["tcp/localhost:7447"]

        # Build return structure
        result = {
            "nats": {
                "urls": urls,
                "jwt": jwt,
                "nkey_seed": seed
            }
        }

        # If URLs use tls://, add TLS configuration
        if any(url.startswith("tls://") for url in urls):
            ca_file = os.getenv("MESSAGING_TLS_CA_FILE") or os.getenv("NATS_TLS_CA_FILE")
            cert_file = os.getenv("MESSAGING_TLS_CERT_FILE") or os.getenv("NATS_TLS_CERT_FILE")
            key_file = os.getenv("MESSAGING_TLS_KEY_FILE") or os.getenv("NATS_TLS_KEY_FILE")

            if not ca_file:
                logging.getLogger(__name__).warning(
                    "TLS URLs detected but no CA certificate configured. "
                    "Set MESSAGING_TLS_CA_FILE or NATS_TLS_CA_FILE."
                )

            tls_config = {"ca_file": ca_file}
            if cert_file and key_file:
                tls_config["cert_file"] = cert_file
                tls_config["key_file"] = key_file

            result["nats"]["tls"] = tls_config

        return result

    def _load_factory_identity(self, identity_file: str) -> dict:
        """Load factory identity JSON file."""
        identity_path = Path(identity_file)
        if not identity_path.exists():
            raise FileNotFoundError(f"Factory identity file not found: {identity_file}")

        with open(identity_path) as f:
            identity = json.load(f)

        # Validate required fields
        required_fields = ['device_id', 'device_type', 'capabilities', 'provisioning']
        for field in required_fields:
            if field not in identity:
                raise ValueError(f"Factory identity missing required field: {field}")

        return identity

    def _is_commissioned(self) -> bool:
        """Check if device has been commissioned."""
        if not self._factory_identity:
            return False
        return self._factory_identity.get('provisioning', {}).get('commissioned', False)

    def _get_credentials_path_from_identity(self) -> str:
        """Determine credentials file path from factory identity."""
        if not self._factory_identity:
            raise ValueError("No factory identity loaded")

        device_id = self._factory_identity['device_id']
        # Default to security_infra/credentials/{device_id}.creds
        return f"security_infra/credentials/{device_id}.creds"

    def _validate_startup_config(self) -> None:
        """
        Validate startup configuration and provide helpful error messages.

        Checks for common misconfigurations before attempting to connect.
        """
        # Skip validation in insecure mode (for development/testing)
        if self.allow_insecure:
            self._logger.warning(
                "Running in INSECURE mode (DEVICE_CONNECT_ALLOW_INSECURE=true). "
                "Do NOT use this in production!"
            )
            return

        issues = []
        warnings = []

        # Check 0: No credentials configured at all (most common error)
        if not self.nats_jwt and not os.getenv("NATS_CREDENTIALS_FILE"):
            issues.append(
                "No authentication credentials configured.\n"
                "  NATS server requires authentication but no credentials were provided.\n\n"
                "  You need to set these environment variables:\n"
                "    - DEVICE_ID (must match your device credentials)\n"
                "    - NATS_URL (must use tls:// for secure connections)\n"
                "    - MESSAGING_TLS_CA_FILE (path to CA certificate)\n"
                "    - NATS_CREDENTIALS_FILE (path to device credentials)\n\n"
                "  Quick fix:\n"
                "    export DEVICE_ID=camera-001\n"
                "    export NATS_URL=tls://localhost:4222\n"
                "    export MESSAGING_TLS_CA_FILE=security_infra/certs/ca-cert.pem\n"
                "    export NATS_CREDENTIALS_FILE=security_infra/credentials/camera-001.creds"
            )

        # Check 1: TLS URL without CA file
        uses_tls = any(url.startswith("tls://") for url in self.messaging_urls)
        if uses_tls and not self.tls_ca_file:
            issues.append(
                "URL uses 'tls://' but MESSAGING_TLS_CA_FILE is not set.\n"
                "  Fix: export MESSAGING_TLS_CA_FILE=security_infra/certs/ca-cert.pem"
            )

        # Check 2: Non-TLS URL with CA file (warning, not error)
        if not uses_tls and self.tls_ca_file:
            warnings.append(
                "MESSAGING_TLS_CA_FILE is set but URL does not use 'tls://'.\n"
                "TLS will not be used. Change URL to 'tls://...' to enable TLS."
            )

        # Check 3: JWT without NKey seed or vice versa
        if bool(self.nats_jwt) != bool(self.nats_nkey_seed):
            issues.append(
                "JWT authentication requires BOTH jwt and nkey_seed.\n"
                f"  Currently: jwt={'present' if self.nats_jwt else 'missing'}, "
                f"nkey_seed={'present' if self.nats_nkey_seed else 'missing'}"
            )

        # Check 4: No authentication configured for TLS connection
        if uses_tls and not self.nats_jwt:
            warnings.append(
                "Using TLS but no JWT authentication configured.\n"
                "This is likely an error unless NATS is configured for anonymous access."
            )

        # Check 5: Validate required environment variables are set for common scenarios
        if os.getenv("NATS_CREDENTIALS_FILE"):
            # User is trying to use credentials file
            if not self.nats_jwt or not self.nats_nkey_seed:
                issues.append(
                    "NATS_CREDENTIALS_FILE is set but credentials were not loaded.\n"
                    "  Make sure the file exists and is valid."
                )

        # Show warnings (non-fatal)
        for warning in warnings:
            self._logger.warning(f"\n⚠️  {warning}")

        # Report all issues
        if issues:
            error_msg = (
                f"\n{'='*70}\n"
                f"❌ CONFIGURATION ERRORS DETECTED\n"
                f"{'='*70}\n"
                f"Please fix the following issues:\n\n"
            )
            for i, issue in enumerate(issues, 1):
                error_msg += f"{i}. {issue}\n\n"

            error_msg += (
                f"Current configuration:\n"
                f"  DEVICE_ID={self.device_id}\n"
                f"  NATS_URL={os.getenv('NATS_URL', 'not set')}\n"
                f"  NATS_CREDENTIALS_FILE={os.getenv('NATS_CREDENTIALS_FILE', 'not set')}\n"
                f"  MESSAGING_TLS_CA_FILE={os.getenv('MESSAGING_TLS_CA_FILE') or os.getenv('NATS_TLS_CA_FILE', 'not set')}\n\n"
                f"Example correct configuration:\n"
                f"  export DEVICE_ID=camera-001\n"
                f"  export NATS_URL=tls://localhost:4222\n"
                f"  export MESSAGING_TLS_CA_FILE=security_infra/certs/ca-cert.pem\n"
                f"  export NATS_CREDENTIALS_FILE=security_infra/credentials/camera-001.creds\n"
                f"{'='*70}\n"
            )
            raise ValueError(error_msg)

    async def _run_commissioning(self) -> str:
        """
        Run commissioning mode and return path to commissioned credentials.

        Returns:
            Path to credentials file
        """
        try:
            from device_connect_sdk.security.commissioning import CommissioningMode
        except ImportError:
            try:
                from fabric.security.commissioning import CommissioningMode
            except ImportError:
                raise ImportError(
                    "Commissioning requires device-connect-server[security]. "
                    "Install with: pip install 'device-connect-server[security]'"
                )

        if not self._factory_identity:
            raise ValueError("Factory identity required for commissioning")

        device_id = self._factory_identity['device_id']
        device_type = self._factory_identity['device_type']
        capabilities = self._factory_identity['capabilities']
        factory_pin = self._factory_identity['provisioning']['pin']
        nkey_public = self._factory_identity.get('nkey', {}).get('public_key')
        nkey_seed = self._factory_identity.get('nkey', {}).get('seed')

        self._logger.info("Device not commissioned, entering commissioning mode...")

        # Create commissioning mode handler
        commissioning = CommissioningMode(
            device_id=device_id,
            device_type=device_type,
            factory_pin=factory_pin,
            capabilities=capabilities,
            nkey_public=nkey_public,
            nkey_seed=nkey_seed,
            port=self.commissioning_port
        )

        # Start commissioning server and wait for admin
        credentials = await commissioning.start_commissioning_server()

        # Determine credentials file path
        creds_path = self._get_credentials_path_from_identity()

        # Save credentials
        commissioning.save_credentials(credentials, path=creds_path)

        # Update identity file to mark as commissioned
        self._factory_identity['provisioning']['commissioned'] = True
        import datetime
        self._factory_identity['provisioning']['commissioned_at'] = datetime.datetime.now(datetime.UTC).isoformat().replace('+00:00', 'Z')

        with open(self.factory_identity_file, 'w') as f:
            json.dump(self._factory_identity, f, indent=2)

        self._logger.info("✅ Device commissioned! Credentials saved to %s", creds_path)

        return creds_path

    def _sign_nonce(self, nonce: bytes | str) -> bytes:
        """Sign nonce with NKey for JWT authentication."""
        import base64

        try:
            import nkeys
        except ImportError:
            raise ImportError("nkeys library required for JWT auth. Install: pip install nkeys")

        if not self.nats_nkey_seed:
            raise ValueError("NKey seed required for JWT signature")

        # Handle both bytes and str nonce (NATS may send either)
        if isinstance(nonce, str):
            nonce = nonce.encode()

        # Create keypair from seed
        kp = nkeys.from_seed(self.nats_nkey_seed.encode())

        # Sign the nonce and return base64-encoded signature
        signature = kp.sign(nonce)
        return base64.b64encode(signature)


    def set_heartbeat_provider(self, provider: Callable[[], dict]) -> None:
        """A way to supply dynamic info within the heartbeat events."""

        self._heartbeat_provider = provider


    def add_connection_listener(self, callback: Callable[[bool], Awaitable[None]]) -> None:
        """Register a coroutine to be notified when the NATS connection state changes, e.g. on disconnect."""

        self._connection_callbacks.append(callback)

    def add_registration_listener(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Register a coroutine to be notified when the device is successfully registered."""

        self._registration_callbacks.append(callback)


    async def enqueue_event(self, event: str, payload: dict) -> None:
        """Enqueue a JSON-RPC notification for a custom event."""
        #TODO: Modify to put events that are about to be emitted through a check for local recipes, that may or may not stop/modify the event before it leaves the device.

        note = {"jsonrpc": "2.0", "method": event, "params": payload}
        subj = f"device-connect.{self.tenant}.{self.device_id}.event.{event}"
        try:
            self._event_queue.put_nowait((subj, json.dumps(note).encode()))
        except asyncio.QueueFull:
            self._logger.warning("Event queue full (maxsize=10000), dropping oldest event")
            try:
                self._event_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._event_queue.put_nowait((subj, json.dumps(note).encode()))


    async def _register(self, force: bool = False) -> None:
        """Register the device with the Device Connect registry, retrying on failure."""

        if not force and self._registration_id and self._registration_expires_at > time.time():
            self._logger.debug(
                "Skipping registration; existing registration %s valid for %.1fs",
                self._registration_id,
                self._registration_expires_at - time.time(),
            )
            return

        # Use lock to prevent duplicate concurrent registrations
        if self._registration_lock is None:
            self._registration_lock = asyncio.Lock()

        if self._registration_lock.locked():
            self._logger.debug("Registration already in progress, skipping")
            return

        async with self._registration_lock:
            # Double-check after acquiring lock (another task may have just registered)
            if not force and self._registration_id and self._registration_expires_at > time.time():
                self._logger.debug("Registration completed by another task, skipping")
                return

            delay = 1 # initial retry delay in seconds
            while True:
                req_id = f"{self.device_id}-{int(time.time()*1000)}"
                # Get capabilities dynamically from driver if available (supports runtime capability loading)
                caps = self._driver.capabilities if self._driver else self.capabilities
                params = {
                    "device_id": self.device_id,
                    "device_ttl": self.ttl,
                    "capabilities": caps.model_dump(),
                    "identity": self.identity,
                    "status": {
                        **self.status,
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    },
                }
                try:
                    self._logger.info("Registering device")
                    response_data = await self.messaging.request(
                        f"device-connect.{self.tenant}.registry",
                        json.dumps({"jsonrpc": "2.0", "id": req_id, "method": "registerDevice", "params": params}).encode(),
                        timeout=2,
                    )
                    self._handle_registration_reply(response_data)
                    # Note: device/online event is published by the registry service
                    break
                except Exception as e:
                    self._logger.warning("Registration failed: %s; retrying in %ss", e, delay)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)


    async def _heartbeat_loop(self) -> None:
        """Emit heartbeat events at regular intervals, re-registering if needed."""

        subj = f"device-connect.{self.tenant}.{self.device_id}.heartbeat"
        last_ok = time.time()
        self._logger.debug(f"Heartbeat loop started, subject={subj}, interval={self._heartbeat_interval}")
        while True:
            # Build heartbeat payload
            beat = {"device_id": self.device_id, "ts": time.time()}
            self._logger.debug(f"Heartbeat loop iteration, beat={beat}")
            if self._heartbeat_provider:
                try:
                    beat.update(self._heartbeat_provider())
                except Exception as e:  # pragma: no cover - provider may fail
                    self._logger.warning("Heartbeat provider error: %s", e)

            # Ensure messaging connectivity
            if not self.messaging.is_connected:
                self._logger.info("Waiting for messaging reconnect")
                if self.messaging.is_closed:
                    await self._connect_messaging()
                while not self.messaging.is_connected:
                    await asyncio.sleep(1)
                if not self._d2d_mode:
                    try:
                        await self._register(force=True)
                    except Exception as e:
                        self._logger.error("Device re-registration failed after reconnect: %s", e)

            # Send heartbeat
            try:
                self._logger.debug(f"Publishing heartbeat to {subj}")
                await self.messaging.publish(subj, json.dumps(beat).encode())
                self._logger.debug("Heartbeat sent successfully")
                last_ok = time.time()
                self._registration_expires_at = last_ok + self.ttl
            except Exception as e:
                self._logger.warning("Heartbeat send failed: %s", e)
                if time.time() - last_ok > self.ttl and not self._d2d_mode:
                    # Re-register if no successful heartbeat within TTL
                    try:
                        await self._register(force=True)
                        last_ok = time.time()
                    except Exception as e2:
                        self._logger.error("Device re-registration failed after heartbeat error: %s", e2)

            await asyncio.sleep(self._heartbeat_interval)


    async def _cmd_subscription(self) -> None:
        """Subscribe to JSON-RPC commands and dispatch to registered handlers."""

        subj = f"device-connect.{self.tenant}.{self.device_id}.cmd"

        async def on_msg(data: bytes, reply_subject: Optional[str]):
            """Handle incoming JSON-RPC command messages."""
            try:
                payload = json.loads(data)
                method = payload.get("method")
                if "id" not in payload:
                    return

                params_dict = payload.get("params", {})

                # Extract trace metadata for cross-device RPC correlation
                dc_meta = params_dict.pop("_dc_meta", {})
                source_device = dc_meta.get("source_device")

                # Extract OTel context from _dc_meta (W3C traceparent/tracestate)
                parent_ctx = extract_from_meta(dc_meta)

                # Invoke through driver
                if self._driver is None:
                    if reply_subject:
                        await self.messaging.publish(
                            reply_subject,
                            build_rpc_error(payload["id"], -32601, "No driver configured")
                        )
                    return

                driver_functions = self._driver._get_functions()
                if method not in driver_functions:
                    if reply_subject:
                        await self.messaging.publish(
                            reply_subject,
                            build_rpc_error(payload["id"], -32601, f"Unknown method: {method}")
                        )
                    return

                try:
                    tracer = get_tracer()
                    # Start SERVER span with parent context from caller
                    with tracer.start_as_current_span(
                        f"device.handle_cmd/{method}",
                        context=parent_ctx,
                        kind=SpanKind.SERVER,
                        attributes={
                            "device_connect.device.id": self.device_id,
                            "rpc.method": method,
                            "device_connect.source_device": source_device or "",
                        },
                    ):
                        # Pass source_device to driver for logging (existing pattern)
                        if source_device:
                            params_dict["source_device"] = source_device
                        result = await self._driver.invoke(method, **params_dict)
                    if reply_subject:
                        await self.messaging.publish(
                            reply_subject,
                            build_rpc_response(payload["id"], result)
                        )
                except Exception as e:
                    self._logger.error("Driver function %s failed: %s", method, e)
                    if reply_subject:
                        await self.messaging.publish(
                            reply_subject,
                            build_rpc_error(payload["id"], -32000, str(e))
                        )
            except Exception as e:  # pragma: no cover - best effort logging
                self._logger.exception("Command handler error: %s", e)

        await self.messaging.subscribe(subj, callback=on_msg)
        self._logger.info("Subscribed to commands on %s", subj)


    async def _event_dispatch_loop(self) -> None:
        """Send queued events, retrying on failure."""

        while True:
            subj, data = await self._event_queue.get()
            delay = 1
            while True:
                try:
                    await self.messaging.publish(subj, data)
                    delay = 1
                    break
                except Exception as e:  # pragma: no cover - network may be down
                    self._logger.warning(
                        "Event dispatch to %s failed: %s; retrying in %ss",
                        subj,
                        e,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)
            self._event_queue.task_done()


    async def _notify_conn_state(self, state: bool) -> None:
        """Notify registered callbacks of a connection state change."""

        for cb in list(self._connection_callbacks):
            try:
                await cb(state)
            except Exception as e:  # pragma: no cover - callbacks are user code
                self._logger.exception("Connection callback error: %s", e)


    async def _connect_messaging(self) -> None:
        """Establish a resilient connection to the messaging broker."""

        async def on_disconnect():
            self._logger.warning(f"{self._messaging_backend.upper()} disconnected")
            await self._notify_conn_state(False)

        async def on_reconnect():
            self._logger.info(f"{self._messaging_backend.upper()} reconnected")
            if not self._d2d_mode:
                asyncio.create_task(self._register(force=True))
            await self._notify_conn_state(True)

        # Create messaging client based on backend
        self.messaging = create_client(self._messaging_backend)
        self._logger.info(f"Using {self._messaging_backend.upper()} messaging backend")

        delay = 2
        max_startup_retries = 3
        startup_attempts = 0

        while True:
            try:
                # Connect using messaging abstraction layer
                await self.messaging.connect(
                    servers=self.messaging_urls,
                    credentials=self.messaging_auth,
                    tls_config=self.messaging_tls,
                    reconnect_cb=on_reconnect,
                    disconnect_cb=on_disconnect,
                    reconnect_time_wait=2,
                    max_reconnect_attempts=-1
                )
                await self._notify_conn_state(True)
                self._logger.info(
                    f"Connected to {self._messaging_backend.upper()} broker: {self.messaging_urls}"
                )
                break
            except Exception as e:
                error_str = str(e)
                startup_attempts += 1

                # Provide helpful error messages for common issues
                backend_name = (self._messaging_backend or "zenoh").upper()
                if "Authorization Violation" in error_str or "authorization violation" in error_str.lower():
                    self._logger.error(
                        f"\n{'='*70}\n"
                        f"❌ {backend_name} AUTHORIZATION ERROR\n"
                        f"{'='*70}\n"
                        f"Failed to authenticate with {backend_name} server.\n\n"
                        f"Common causes:\n"
                        f"  1. Missing credentials or TLS certificates\n"
                        f"  2. DEVICE_ID mismatch (see permissions violation errors)\n"
                        f"  3. Device not commissioned yet\n\n"
                        f"Current configuration:\n"
                        f"  DEVICE_ID={self.device_id}\n"
                        f"  MESSAGING_BACKEND={self._messaging_backend}\n"
                        f"  MESSAGING_TLS_CA_FILE={os.getenv('MESSAGING_TLS_CA_FILE', 'not set')}\n\n"
                        f"To fix (Zenoh mTLS):\n"
                        f"  ./security_infra/generate_tls_certs.sh --client <device-id>\n"
                        f"  export MESSAGING_TLS_CA_FILE=security_infra/ca.pem\n"
                        f"  export MESSAGING_TLS_CERT_FILE=security_infra/<device-id>-cert.pem\n"
                        f"  export MESSAGING_TLS_KEY_FILE=security_infra/<device-id>-key.pem\n\n"
                        f"To fix (NATS JWT):\n"
                        f"  export NATS_CREDENTIALS_FILE=~/.device-connect/credentials/<device-id>.creds.json\n"
                        f"{'='*70}\n"
                    )
                    # Don't retry authorization errors indefinitely during startup
                    if startup_attempts >= max_startup_retries:
                        raise RuntimeError(
                            "Failed to connect after multiple authorization errors. "
                            "Please check your credentials configuration."
                        ) from e
                elif "permissions violation" in error_str.lower():
                    self._logger.error(
                        f"\n{'='*70}\n"
                        f"❌ {backend_name} PERMISSIONS ERROR\n"
                        f"{'='*70}\n"
                        f"Connected to {backend_name} but don't have permission to publish/subscribe.\n\n"
                        f"This usually means DEVICE_ID doesn't match your credentials.\n\n"
                        f"Current DEVICE_ID: {self.device_id}\n\n"
                        f"Check your credentials file for the correct device_id and set:\n"
                        f"  export DEVICE_ID=<device-id-from-credentials>\n"
                        f"{'='*70}\n"
                    )
                    if startup_attempts >= max_startup_retries:
                        raise RuntimeError(
                            "Failed to connect after multiple permission errors. "
                            "Please verify DEVICE_ID matches your credentials."
                        ) from e
                else:
                    self._logger.warning("%s connect failed: %s; retrying in %ss", backend_name, e, delay)

                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)


    async def run(self) -> None:
        """
        Run the device.

        If factory_identity_file is provided and device is not commissioned:
            1. Enter commissioning mode
            2. Wait for admin to commission
            3. Save credentials
            4. Transition to operational mode

        If commissioned or no factory identity:
            1. Connect to messaging broker
            2. Register with registry
            3. Start heartbeat, command subscription, event dispatch
        """
        # Handle commissioning if needed
        entering_commissioning = False
        if self.factory_identity_file and self.auto_commission:
            if not self._is_commissioned():
                # Mark that we're entering commissioning mode
                entering_commissioning = True
                # Enter commissioning mode
                creds_path = await self._run_commissioning()

                # Load the newly commissioned credentials
                creds = self._load_credentials(creds_path)
                creds_nats = creds.get("nats", {})
                self.messaging_urls = creds_nats.get("urls", self.messaging_urls)
                self.nats_jwt = creds_nats.get("jwt")
                self.nats_nkey_seed = creds_nats.get("nkey_seed")
                tls_config = creds_nats.get("tls", {})
                self.tls_ca_file = tls_config.get("ca_file", self.tls_ca_file)

                # Update messaging auth with JWT credentials
                self.messaging_auth = {
                    "jwt": self.nats_jwt,
                    "nkey_seed": self.nats_nkey_seed,
                    "signature_cb": self._sign_nonce
                }

                # Update messaging TLS config (only if CA file is present)
                if self.tls_ca_file:
                    self.messaging_tls = {"ca_file": self.tls_ca_file}
                    cert_file = tls_config.get("cert_file")
                    key_file = tls_config.get("key_file")
                    if cert_file and key_file:
                        self.messaging_tls["cert_file"] = cert_file
                        self.messaging_tls["key_file"] = key_file
                else:
                    self.messaging_tls = None

                self._logger.info("Transitioning to operational mode...")

        # Validate configuration before attempting to connect
        # (Skip validation if we just completed commissioning)
        if not entering_commissioning:
            self._validate_startup_config()

        # Auto-initialize OpenTelemetry (zero-config for device developers)
        # Uses OTEL_EXPORTER_OTLP_ENDPOINT env var if set, otherwise no-op
        device_type = self._driver.device_type if self._driver else "unknown"
        try:
            telemetry = DeviceConnectTelemetry(
                service_name=f"device-connect-device-{self.device_id}",
                device_id=self.device_id,
                device_type=device_type,
                tenant=self.tenant,
            )
            telemetry.setup_otlp_exporter()
            self._logger.debug("OpenTelemetry initialized for device %s", self.device_id)
        except Exception as e:
            self._logger.debug("OpenTelemetry initialization skipped: %s", e)

        # Operational mode
        await self._connect_messaging()
        self._logger.info("Connected to messaging backend: %s", self.messaging_urls)

        # Connect driver if present
        if self._driver is not None:
            await self._driver.connect()
            # Wire up driver's event emitter to our event queue
            self._driver.set_event_callback(self._on_driver_event)
            self._logger.info("Driver connected: %s", self._driver.device_type)

            # Set up DeviceDriver capabilities (router, registry, subscriptions)
            await self._setup_agentic_driver()

            # Start device routines (@periodic decorated methods)
            await self._driver._start_routines()

        if self._d2d_mode:
            self._logger.info("D2D mode: skipping registry registration, using presence announcements")
        else:
            await self._register(force=True)

        # Subscribe to commands BEFORE capability routines so log order makes sense
        await self._cmd_subscription()

        # Start capability routines if driver supports them (CapabilityDriverMixin)
        # This must happen after registration so events don't fire before device is registered
        if hasattr(self._driver, 'start_capability_routines'):
            await self._driver.start_capability_routines()

        # Start D2D presence announcer and collector if in D2D mode.
        # The collector may already exist if _setup_agentic_driver() created
        # one for the D2DRegistry — reuse it instead of creating a duplicate.
        if self._d2d_mode:
            from device_connect_sdk.discovery import PresenceAnnouncer, PresenceCollector
            caps = self._driver.capabilities if self._driver else self.capabilities
            if self._d2d_collector is None:
                self._d2d_collector = PresenceCollector(self.messaging, self.tenant, device_id=self.device_id)
            self._d2d_announcer = PresenceAnnouncer(
                self.messaging,
                device_id=self.device_id,
                tenant=self.tenant,
                capabilities=caps.model_dump() if hasattr(caps, 'model_dump') else caps,
                identity=self.identity,
                status=self.status,
            )
            # Wire up burst trigger BEFORE starting the collector so that
            # peers discovered immediately on start already trigger bursts.
            self._d2d_collector._on_new_peer = lambda _pid: self._d2d_announcer.trigger_burst()
            if not self._d2d_collector._started:
                await self._d2d_collector.start()
            await self._d2d_announcer.start()

            # Configure D2D retry in messaging adapter
            if hasattr(self.messaging, 'configure_d2d_retry'):
                self.messaging.configure_d2d_retry(retries=3, delay=0.3)

        # Gate startup on depends_on device types if the driver declares them.
        # Runs after the presence collector is started so announcements are
        # already being received.  Works in both D2D and registry modes.
        if self._driver and getattr(self._driver, 'depends_on', ()):
            depends_timeout = float(os.getenv("DEVICE_CONNECT_DEPENDS_TIMEOUT", "30"))
            for dtype in self._driver.depends_on:
                self._logger.info("Waiting for dependency: device_type=%s (timeout=%.0fs)", dtype, depends_timeout)
                await self._driver.wait_for_device(device_type=dtype, timeout=depends_timeout)
                self._logger.info("Dependency satisfied: device_type=%s", dtype)

        # Track background tasks so we can cancel them on shutdown
        self._background_tasks = [
            asyncio.create_task(self._heartbeat_loop()),
            asyncio.create_task(self._event_dispatch_loop()),
        ]

        try:
            # Store future so stop() can cancel it
            self._run_future = asyncio.get_event_loop().create_future()
            await self._run_future  # Run forever until stop() is called
        except asyncio.CancelledError:
            self._logger.debug("Device run cancelled, cleaning up...")
            raise
        finally:
            # Cancel all background tasks
            for task in self._background_tasks:
                if not task.done():
                    task.cancel()

            # Wait for tasks to complete cancellation (with timeout)
            if self._background_tasks:
                await asyncio.gather(*self._background_tasks, return_exceptions=True)

            # Announce departure before tearing down D2D presence
            if self._d2d_announcer and self.messaging and not self.messaging.is_closed:
                try:
                    departure_subject = f"device-connect.{self.tenant}.{self.device_id}.presence"
                    departure_payload = json.dumps({
                        "device_id": self.device_id,
                        "tenant": self.tenant,
                        "departing": True,
                        "ts": time.time(),
                    }).encode()
                    await self.messaging.publish(departure_subject, departure_payload)
                except Exception:
                    self._logger.debug("Failed to publish departure announcement", exc_info=True)

            # Stop D2D presence components
            if self._d2d_announcer:
                await self._d2d_announcer.stop()
            if self._d2d_collector:
                await self._d2d_collector.stop()

            # Cleanup driver on shutdown
            if self._driver is not None:
                # Stop device routines (@periodic decorated methods)
                await self._driver._stop_routines()

                # Teardown DeviceDriver subscriptions if applicable
                await self._teardown_agentic_driver()
                await self._driver.disconnect()
                self._logger.info("Driver disconnected")

            # Close messaging connection
            if self.messaging is not None and not self.messaging.is_closed:
                await self.messaging.close()
                self._logger.debug("Messaging connection closed")

    async def stop(self) -> None:
        """Stop the device gracefully.

        Cancels the run() loop and allows cleanup to proceed.
        """
        if self._run_future is not None and not self._run_future.done():
            self._run_future.cancel()
            # Wait for cleanup to complete
            try:
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                pass

    async def invoke(
        self,
        device_id: str,
        function: str,
        timeout: Optional[float] = None,
        **params: Any
    ) -> Dict[str, Any]:
        """Invoke a function on another device (D2D call).

        Delegates to the driver's invoke_remote() if available (DeviceDriver).

        Args:
            device_id: Target device identifier
            function: Function name to invoke
            timeout: Optional timeout in seconds
            **params: Function parameters

        Returns:
            Response dict with 'result' or 'error'

        Raises:
            RuntimeError: If driver doesn't support remote invocation
        """
        if hasattr(self._driver, 'invoke_remote'):
            return await self._driver.invoke_remote(
                device_id=device_id,
                function_name=function,
                timeout=timeout,
                **params
            )
        raise RuntimeError(
            f"Driver {type(self._driver).__name__} doesn't support D2D invocation. "
            "Use DeviceDriver for D2D capabilities."
        )

    async def _on_driver_event(self, event_name: str, payload: dict) -> None:
        """Callback for events emitted by the driver."""
        await self.enqueue_event(event_name, payload)

    async def _setup_agentic_driver(self) -> None:
        """Set up DeviceDriver with router and registry for D2D capabilities.

        This method configures the driver with the necessary dependencies
        for remote invocation and event subscription capabilities.
        D2D is now built into the base DeviceDriver.
        """
        if self._driver is None:
            return

        # Import DeviceDriver at runtime to avoid circular imports
        try:
            from device_connect_sdk.drivers.base import DeviceDriver
        except ImportError:
            return

        if not isinstance(self._driver, DeviceDriver):
            return

        self._logger.info("Setting up DeviceDriver D2D capabilities")

        # Create and set D2D router (inline — no orchestration dependency).
        router = _D2DRouter(
            self.messaging,
            tenant=self.tenant,
        )
        self._driver.router = router
        self._logger.debug("D2D router configured for DeviceDriver")

        # Pass device context to driver for capability runtime
        self._driver._device_id = self.device_id
        self._driver._nats_url = self.messaging_urls[0] if self.messaging_urls else None

        # Create and set registry (RegistryClient or D2DRegistry)
        if self._d2d_mode:
            from device_connect_sdk.discovery import D2DRegistry, PresenceCollector
            # Reuse the collector from run() if available, otherwise create one.
            # Don't start it here — run() will start it after wiring _on_new_peer
            # so that burst announcements work from the moment discovery begins.
            collector = getattr(self, '_d2d_collector', None)
            if collector is None:
                collector = PresenceCollector(self.messaging, self.tenant, device_id=self.device_id)
                self._d2d_collector = collector
            self._driver.registry = D2DRegistry(collector)
            self._logger.debug("D2DRegistry configured for DeviceDriver (no infrastructure)")
        else:
            try:
                try:
                    from device_connect_sdk.registry.client import RegistryClient
                except ImportError:
                    from fabric.registry.client import RegistryClient
                from device_connect_sdk.messaging.config import MessagingConfig
                config = MessagingConfig(
                    backend=self._messaging_backend or "zenoh",
                    servers=self.messaging_urls,
                )
                registry = RegistryClient(self.messaging, config, tenant=self.tenant)
                self._driver.registry = registry
                self._logger.debug("RegistryClient configured for DeviceDriver")
            except ImportError:
                self._logger.debug("RegistryClient not available (device-connect-server not installed)")

        # Set up event subscriptions
        await self._driver.setup_subscriptions()
        self._logger.info("DeviceDriver D2D setup complete")

    async def _teardown_agentic_driver(self) -> None:
        """Teardown DeviceDriver subscriptions if applicable."""
        if self._driver is None:
            return

        # Import DeviceDriver at runtime to avoid circular imports
        try:
            from device_connect_sdk.drivers.base import DeviceDriver
        except ImportError:
            return

        if not isinstance(self._driver, DeviceDriver):
            return

        self._logger.debug("Tearing down DeviceDriver subscriptions")
        await self._driver.teardown_subscriptions()

    def _handle_registration_reply(self, data: bytes) -> None:
        """Parse registry response and update local registration metadata."""

        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Malformed registration response") from exc

        if "error" in payload:
            error = payload["error"] or {}
            raise RuntimeError(error.get("message", "registration failed"))

        result = payload.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Missing registration result in response")

        registration_id = result.get("device_registration_id")
        if not registration_id:
            raise RuntimeError("Registry did not return device_registration_id")
        try:
            registration_uuid = uuid.UUID(str(registration_id))
        except ValueError as exc:
            raise RuntimeError("Invalid device_registration_id received") from exc

        self._registration_id = str(registration_uuid)
        self._registration_expires_at = time.time() + self.ttl
        self._logger.info(
            "Device registered: registration_id=%s",
            self._registration_id,
        )

        # Notify registration callbacks
        for cb in list(self._registration_callbacks):
            try:
                asyncio.create_task(cb())
            except Exception as e:
                self._logger.exception("Registration callback error: %s", e)



"""
Device Registry Service.

Responsibilities:
1. Listen for JSON-RPC 'registerDevice' requests over messaging broker.
2. Persist device info in etcd with a TTL lease.
3. Listen for heartbeat messages and refresh the lease.
4. Support pluggable messaging backends (NATS, MQTT).

Multi-tenant: set TENANTS (comma-separated) to handle multiple tenants
in a single process.  Falls back to TENANT for backward compatibility.
"""
import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, List, Optional

from pydantic import BaseModel, Field

import logging

from device_connect_edge import DeviceCapabilities, build_rpc_error, build_rpc_response
from device_connect_edge.messaging import MessagingClient, create_client
from device_connect_edge.messaging.config import MessagingConfig

from device_connect_server.registry.service import registry
from device_connect_server.registry.service.registry import summarize_fleet
from device_connect_server.security.acl import ACLManager


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("device_registry_service")

# Configuration from environment
TENANT = os.getenv("TENANT", "default")
TENANTS_RAW = os.getenv("TENANTS", "")
MESSAGING_BACKEND = os.getenv("MESSAGING_BACKEND")  # Auto-detect if not specified
NATS_CREDENTIALS_FILE = os.getenv("NATS_CREDENTIALS_FILE")
NATS_URLS = os.getenv("NATS_URLS", "")
NATS_URL = os.getenv("NATS_URL", "")

# This holds heartbeat timestamp and ttl per device (keyed by "tenant/device_id")
_last_seen: dict[str, float] = {}
_device_ttl: dict[str, int] = {}


def _resolve_tenants() -> List[str]:
    """Resolve the list of tenants to handle.

    Uses TENANTS env var (comma-separated) if set, otherwise falls back
    to the single TENANT env var for backward compatibility.
    """
    if TENANTS_RAW.strip():
        return [t.strip() for t in TENANTS_RAW.split(",") if t.strip()]
    return [TENANT]


def _parse_creds_file(creds_path: Path) -> dict[str, Any]:
    """
    Parse NATS credentials file (supports both .creds and JSON formats).

    .creds format (nsc generated):
        -----BEGIN NATS USER JWT-----
        <jwt-string>
        ------END NATS USER JWT------

        -----BEGIN USER NKEY SEED-----
        <seed-string>
        ------END USER NKEY SEED------

    JSON format (custom):
        {
            "tenant": "default",
            "nats": {
                "urls": ["tls://nats:4222"],
                "jwt": "<jwt-string>",
                "nkey_seed": "<seed-string>",
                "tls": {"ca_file": "/certs/ca-cert.pem"}
            }
        }
    """
    with creds_path.open() as fh:
        content = fh.read()

    # Try to parse as JSON first
    try:
        return json.loads(content)
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

    # Determine URLs from env var
    urls = []
    if NATS_URLS:
        urls = [url.strip() for url in NATS_URLS.split(",") if url.strip()]
    elif NATS_URL:
        urls = [NATS_URL]
    else:
        urls = ["nats://localhost:4222"]

    # Build return structure
    result = {
        "tenant": "default",  # Default tenant
        "nats": {
            "urls": urls,
            "jwt": jwt,
            "nkey_seed": seed
        }
    }

    # If URLs use tls://, add TLS configuration
    if any(url.startswith("tls://") for url in urls):
        # Check for TLS cert env vars
        ca_file = os.getenv("NATS_TLS_CA_FILE", "/certs/ca-cert.pem")
        cert_file = os.getenv("NATS_TLS_CERT_FILE")
        key_file = os.getenv("NATS_TLS_KEY_FILE")

        tls_config = {"ca_file": ca_file}
        if cert_file and key_file:
            tls_config["cert_file"] = cert_file
            tls_config["key_file"] = key_file

        result["nats"]["tls"] = tls_config

    return result


def _build_messaging_config() -> tuple[str, MessagingClient]:
    """
    Build messaging client configuration from environment variables.

    Returns:
        Tuple of (tenant, messaging_client)
    """
    tenant = TENANT

    # Use MessagingConfig to load configuration from environment
    config = MessagingConfig(
        backend=MESSAGING_BACKEND,
        # servers, credentials, and tls_config loaded from env vars automatically
    )

    # Create messaging client
    messaging = create_client(config.backend)
    logger.info(f"Using {config.backend.upper()} messaging backend")

    # Handle credentials file for tenant override (NATS-specific)
    if NATS_CREDENTIALS_FILE and config.backend == "nats":
        creds_path = Path(NATS_CREDENTIALS_FILE)
        if creds_path.exists():
            creds = _parse_creds_file(creds_path)
            tenant = tenant or creds.get("tenant")

            # Override config with credentials file data
            nats_cfg = creds.get("nats") or {}
            if nats_cfg.get("urls"):
                config.servers = nats_cfg["urls"]

            # Build auth credentials
            jwt = nats_cfg.get("jwt")
            seed = nats_cfg.get("nkey_seed")
            if jwt and seed:
                try:
                    import nkeys  # noqa: F401
                except ImportError as exc:
                    raise RuntimeError(
                        "nkeys library required for JWT authentication. "
                        "Install with 'pip install nkeys'"
                    ) from exc

                # Let the NATS adapter handle JWT signing
                config.credentials = {
                    "jwt": jwt,
                    "nkey_seed": seed
                }
                logger.info("Using JWT credentials for authentication")

            # Build TLS config
            tls_cfg = nats_cfg.get("tls") or {}
            if tls_cfg:
                config.tls_config = tls_cfg
                logger.info("TLS enabled for secure connection")

    tenant = tenant or "default"

    if not config.servers:
        raise RuntimeError(
            "No messaging servers configured. Set MESSAGING_URLS, NATS_URL, "
            "or provide NATS_CREDENTIALS_FILE"
        )

    return tenant, messaging, config

# ---------- Pydantic schema ---------- #
class DeviceIdentity(BaseModel):
    """Immutable device identity (manufacturer, model, arch, etc.)."""
    arch: str = "arm64"
    host_cpu: str = "generic"
    dram_mb: int = 0
    device_type: str | None = None
    manufacturer: str | None = None
    model: str | None = None
    serial_number: str | None = None
    firmware_version: str | None = None
    capabilities: list[str] = Field(default_factory=list)  # e.g., ["camera", "vision"]


class DeviceStatus(BaseModel):
    """Runtime device status (location, availability, etc.)."""
    ts: str
    location: str | None = None
    busy_score: float | None = None
    availability: str | None = None
    task_stack: list[str] | None = None
    battery: int | None = None  # Battery level percentage (0-100)
    online: bool | None = None  # Whether device is online


class RegisterParams(BaseModel):
    device_id: str
    device_ttl: int = Field(gt=0)
    capabilities: DeviceCapabilities  # description, functions, events
    identity: DeviceIdentity          # arch, manufacturer, model, etc.
    status: DeviceStatus              # location, busy_score, availability


# ---------- Per-tenant handler factories ---------- #

def _make_register_handler(tenant: str, messaging: MessagingClient):
    """Create a registerDevice RPC handler bound to ``tenant``."""

    async def rpc_register_device(data: bytes, reply: Optional[str]):
        payload: dict[str, Any] = {}
        try:
            payload = json.loads(data)
            if payload.get("method") != "registerDevice":
                await messaging.publish(
                    reply,
                    build_rpc_error(payload.get("id"), -32601, "method not found"),
                )
                return
            params = RegisterParams(**payload["params"])
            registration_id = str(uuid.uuid4())
            registry_payload = params.model_dump()
            registry_payload.setdefault("registry", {})
            registry_payload["registry"].update(
                {
                    "device_registration_id": registration_id,
                    "registered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            )

            ttl = params.device_ttl
            await asyncio.to_thread(registry.register, tenant, params.device_id, registry_payload, ttl)
            _device_ttl[f"{tenant}/{params.device_id}"] = ttl
            _last_seen[f"{tenant}/{params.device_id}"] = time.time()
            response = {
                "status": "registered",
                "device_registration_id": registration_id,
            }
            await messaging.publish(reply, build_rpc_response(payload["id"], response))
            # emit a device online event
            event_payload = {
                "jsonrpc": "2.0",
                "method": "device/online",
                "params": {
                    "device_id": params.device_id,
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                }
            }
            await messaging.publish(
                f"device-connect.{tenant}.device.online",
                json.dumps(event_payload).encode()
            )
            logger.info("[device-registry] registered %s (tenant=%s) ttl=%s", params.device_id, tenant, ttl)
        except Exception as ex:
            error_code = -32602 if isinstance(ex, (json.JSONDecodeError, ValueError)) else -32603
            await messaging.publish(
                reply,
                build_rpc_error(payload.get("id") if isinstance(payload, dict) else None, error_code, str(ex)),
            )
            params_dict = payload.get("params") if isinstance(payload, dict) else {}
            device_id = params_dict.get("device_id") if isinstance(params_dict, dict) else None
            logger.exception("[device-registry] register %s error", device_id or "<unknown>")

    return rpc_register_device


def _make_list_handler(
    tenant: str,
    messaging: MessagingClient,
    acl_manager: Optional[ACLManager] = None,
):
    """Create a discovery RPC handler bound to ``tenant``.

    Handles:
    - ``discovery/listDevices`` — list devices with optional filters
    - ``discovery/getDevice`` — get a single device by ID (O(1))
    - ``discovery/describeFleet`` — aggregated fleet summary

    If *acl_manager* is provided, results are filtered by the
    ``requester_id`` field in the RPC params.
    """

    async def rpc_discovery(data: bytes, reply: Optional[str]):
        # NOTE: ACL filtering is permissive by default.  When requester_id is
        # omitted (empty string), filter_visible_devices passes all devices
        # because the default visible_to=["*"] wildcard matches any requester.
        # This is intentional — ACL is opt-in and requester_id is supplied by
        # the caller, not extracted from authenticated credentials.
        payload: dict[str, Any] = {}
        try:
            payload = json.loads(data)
            method = payload.get("method", "")
            params = payload.get("params") or {}

            if method == "discovery/listDevices":
                device_type = params.get("device_type")
                location = params.get("location")
                devs = await asyncio.to_thread(
                    registry.list_devices, tenant,
                    device_type=device_type, location=location,
                )
                if acl_manager:
                    requester_id = params.get("requester_id", "")
                    devs = acl_manager.filter_visible_devices(
                        requester_id, devs, tenant=tenant
                    )
                await messaging.publish(
                    reply,
                    build_rpc_response(payload["id"], {"devices": devs})
                )
            elif method == "discovery/getDevice":
                device_id = params.get("device_id")
                if not device_id:
                    await messaging.publish(
                        reply,
                        build_rpc_error(payload.get("id"), -32602, "device_id required")
                    )
                    return
                device = await asyncio.to_thread(
                    registry.get_device, tenant, device_id,
                )
                if device and acl_manager:
                    requester_id = params.get("requester_id", "")
                    visible = acl_manager.filter_visible_devices(
                        requester_id, [device], tenant=tenant
                    )
                    device = visible[0] if visible else None
                await messaging.publish(
                    reply,
                    build_rpc_response(payload["id"], {"device": device})
                )
            elif method == "discovery/describeFleet":
                devs = await asyncio.to_thread(registry.list_devices, tenant)
                if acl_manager:
                    requester_id = params.get("requester_id", "")
                    devs = acl_manager.filter_visible_devices(
                        requester_id, devs, tenant=tenant
                    )
                summary = summarize_fleet(devs)
                await messaging.publish(
                    reply,
                    build_rpc_response(payload["id"], summary)
                )
            else:
                return  # Not a discovery method — ignore
        except Exception as e:
            await messaging.publish(
                reply,
                build_rpc_error(payload.get("id") if isinstance(payload, dict) else None, -32000, str(e))
            )

    return rpc_discovery


def _make_hb_handler(tenant: str):
    """Create a heartbeat handler bound to ``tenant``."""

    async def hb_handler(data_bytes: bytes, reply: Optional[str]):
        data: Any = None
        device_id: str = "?"
        try:
            data = json.loads(data_bytes)
            device_id = data.pop("device_id")
            # Update last_seen immediately (before blocking etcd calls)
            _last_seen[f"{tenant}/{device_id}"] = time.time()
            # Run blocking etcd calls in a thread to avoid blocking the event loop
            await asyncio.to_thread(registry.refresh, tenant, device_id)
            await asyncio.to_thread(registry.update_status, tenant, device_id, data)
            logger.debug("[device-registry] heartbeat from %s (tenant=%s)", device_id, tenant)
        except Exception as e:
            logger.exception("[device-registry] heartbeat error for %s: %s", device_id, e)

    return hb_handler


# ---------- Async entry ---------- #
async def main() -> None:
    _, messaging, config = _build_messaging_config()
    tenants = _resolve_tenants()

    # Connect to messaging broker
    await messaging.connect(
        servers=config.servers,
        credentials=config.credentials,
        tls_config=config.tls_config,
        reconnect_time_wait=2,
        max_reconnect_attempts=-1
    )

    logger.info(
        "[device-registry] connected to %s at %s, tenants=%s",
        config.backend.upper(),
        ", ".join(config.servers) if config.servers else "<unknown>",
        tenants,
    )

    # ACL enforcement (permissive by default — no ACL = visible to all)
    acl_manager = ACLManager()

    # Subscribe per-tenant
    for tenant in tenants:
        await messaging.subscribe(
            f"device-connect.{tenant}.registry",
            queue="registry",
            callback=_make_register_handler(tenant, messaging),
        )
        await messaging.subscribe(
            f"device-connect.{tenant}.discovery",
            queue="orch",
            callback=_make_list_handler(tenant, messaging, acl_manager),
        )
        await messaging.subscribe(
            f"device-connect.{tenant}.*.heartbeat",
            callback=_make_hb_handler(tenant),
        )
        logger.info("[device-registry] listening on tenant=%s", tenant)

    # ---- Watch for device deletes ----
    async def offline_monitor():
        """Periodically check for devices that missed their heartbeat."""
        while True:
            now = time.time()
            for compound_key, ts in list(_last_seen.items()):
                ttl = _device_ttl.get(compound_key)
                if ttl is None:
                    continue
                if now - ts > ttl:
                    # compound_key is "tenant/device_id"
                    tenant_part, device_id = compound_key.split("/", 1)
                    payload = {
                        "jsonrpc": "2.0",
                        "method": "device/offline",
                        "params": {
                            "device_id": device_id,
                            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                        }
                    }
                    await messaging.publish(
                        f"device-connect.{tenant_part}.device.offline",
                        json.dumps(payload).encode()
                    )
                    logger.info("[device-registry] device %s offline (tenant=%s)", device_id, tenant_part)
                    _last_seen.pop(compound_key, None)
                    _device_ttl.pop(compound_key, None)
            await asyncio.sleep(1)

    # NOTE: _monitor_task is intentionally a local variable.  The infinite
    # loop below keeps main() (and therefore this frame) alive, so the task
    # reference is never garbage-collected.
    _monitor_task = asyncio.create_task(offline_monitor())

    # ----- Run forever -----
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    logger.info("[device-registry] starting up...")
    asyncio.run(main())

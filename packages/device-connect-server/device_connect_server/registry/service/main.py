# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

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
# MESSAGING_CREDENTIALS_FILE is the backend-neutral name; NATS_CREDENTIALS_FILE
# is kept as a deprecated fallback (mirrors the edge).
MESSAGING_CREDENTIALS_FILE = os.getenv("MESSAGING_CREDENTIALS_FILE") or os.getenv("NATS_CREDENTIALS_FILE")
NATS_URLS = os.getenv("NATS_URLS", "")
NATS_URL = os.getenv("NATS_URL", "")

# This holds heartbeat timestamp and ttl per device (keyed by "tenant/device_id")
_last_seen: dict[str, float] = {}
_device_ttl: dict[str, int] = {}

_DEFAULT_TTL = 15                # Fallback TTL when device doesn't report one
_PULL_REGISTRATION_TIMEOUT = 5   # Timeout for requestRegistration RPC

# Server-side cap for discovery/listDevices page sizes when the caller
# opts into pagination by passing `limit`. NATS rejects any single
# publish larger than the broker's max_payload, so the registry clamps
# the page size to bound the reply size regardless of what `limit` the
# caller asked for. Empirically a flashlight-auditorium phone record is
# ~13KB serialized, so 200 records ~= 2.6MB, which fits comfortably under
# the 8MB max_payload set in security_infra/setup_deployment.sh while
# keeping per-page round-trip small.
#
# Old clients that omit `limit` entirely fall through to the legacy
# unpaginated reply path — they keep working at small fleet scale and
# fail loudly with `max_payload exceeded` at large scale, the same
# behavior they had before this PR. Silently truncating their reply
# would be worse: they would parse a partial fleet as if it were
# complete and act on stale views. Operators hitting the ceiling should
# upgrade clients to a version that passes `limit`.
_LIST_DEVICES_MAX_LIMIT = int(os.getenv("DC_LIST_DEVICES_MAX_LIMIT", "200"))

# Track which over-cap ``limit`` values we've already warned about so a
# misconfigured client doesn't spam the log on every page. Deduped by the
# requested value, not by caller — the warning is operator-facing
# ("someone is tuning a knob that no longer matches the server cap"),
# not per-request observability. Bounded by the number of distinct
# limits a caller can ask for; in practice 1-2 values.
_WARNED_LIMIT_CLAMPS: set[int] = set()


def _resolve_tenants() -> List[str]:
    """Resolve the list of tenants to handle.

    Uses TENANTS env var (comma-separated) if set, otherwise falls back
    to the single TENANT env var for backward compatibility.
    """
    if TENANTS_RAW.strip():
        return [t.strip() for t in TENANTS_RAW.split(",") if t.strip()]
    return [TENANT]


def _extract_tenant(subject: str) -> str:
    """Extract the tenant from a registry subject/key.

    Handles both the NATS dotted form (``device-connect.{tenant}.registry``)
    and the Zenoh slash-delimited key expression
    (``device-connect/{tenant}/registry``). Without the slash case, every
    Zenoh device was mis-attributed to the ``default`` tenant.
    """
    parts = subject.replace("/", ".").split(".")
    return parts[1] if len(parts) >= 3 else "default"


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
    if MESSAGING_CREDENTIALS_FILE and config.backend == "nats":
        creds_path = Path(MESSAGING_CREDENTIALS_FILE)
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
            "or provide MESSAGING_CREDENTIALS_FILE"
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


# ---------- Shared registration helper ---------- #

async def _do_register(
    tenant: str,
    device_id: str,
    payload: dict,
    ttl: int,
    messaging: MessagingClient,
) -> str:
    """Store device in etcd, update tracking dicts, emit device/online event.

    Returns the generated ``registration_id``.
    """
    registration_id = str(uuid.uuid4())
    payload.setdefault("registry", {})
    payload["registry"].update({
        "device_registration_id": registration_id,
        "registered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    await asyncio.to_thread(registry.register, tenant, device_id, payload, ttl)
    _device_ttl[f"{tenant}/{device_id}"] = ttl
    _last_seen[f"{tenant}/{device_id}"] = time.time()
    online_event = json.dumps({
        "jsonrpc": "2.0",
        "method": "device/online",
        "params": {
            "device_id": device_id,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    }).encode()
    await messaging.publish(
        f"device-connect.{tenant}.device.online", online_event,
    )
    return registration_id


# ---------- Per-tenant handler factories ---------- #

def _make_register_handler(tenant: str, messaging: MessagingClient):
    """Create a registerDevice RPC handler bound to ``tenant``."""

    async def rpc_register_device(data: bytes, reply: Optional[str]):
        if not reply:
            logger.debug("[device-registry] register request with no reply address; ignoring")
            return
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
            registry_payload = params.model_dump()
            registration_id = await _do_register(
                tenant, params.device_id, registry_payload, params.device_ttl, messaging,
            )
            response = {
                "status": "registered",
                "device_registration_id": registration_id,
            }
            await messaging.publish(reply, build_rpc_response(payload.get("id"), response))
            logger.info("[device-registry] registered %s (tenant=%s) ttl=%s", params.device_id, tenant, params.device_ttl)
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
        if not reply:
            logger.debug("[device-registry] discovery request with no reply address; ignoring")
            return
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
                # Pagination: ``offset`` and ``limit`` are optional.
                #
                # If ``limit`` is absent the caller is on the legacy
                # protocol — return the full filtered fleet in a single
                # reply. This preserves backward compatibility (small
                # deployments keep working untouched) and surfaces the
                # original max_payload failure loudly at fleet scale
                # rather than silently truncating to a cap the caller
                # doesn't know about.
                #
                # If ``limit`` is present the caller understands the
                # paged contract: we clamp to ``_LIST_DEVICES_MAX_LIMIT``
                # to bound reply size, return ``next_offset`` and
                # ``total_matched``, and expect the caller to loop.
                #
                # Review notes (do not re-litigate without reading):
                # - Silently clamping legacy callers to the cap was
                #   tried in 2349130 and reverted in 79247e6: a partial
                #   reply with no ``next_offset`` signal is worse than
                #   a loud failure because the caller acts on a
                #   truncated fleet as if it were complete. Operators
                #   hitting the ceiling must upgrade the client.
                # - Streaming replies (multi-message paginated stream)
                #   were considered and rejected in the PR design:
                #   adds reassembly complexity for every caller while
                #   buying nothing the page loop doesn't already get.
                # - ``limit <= 0`` returns -32602 rather than mapping
                #   to the cap (was a review-round-3 fix); the
                #   surprise mapping masked client bugs that passed
                #   unintentional zero/negative values.
                requested_limit = params.get("limit")
                paged = requested_limit is not None
                # ``offset`` is validated up front so a malformed value
                # (e.g. ``"abc"``) produces a clean JSON-RPC error
                # rather than a 500 from int().
                raw_offset = params.get("offset", 0)
                try:
                    offset_val = int(raw_offset or 0)
                except (TypeError, ValueError):
                    await messaging.publish(
                        reply,
                        build_rpc_error(
                            payload.get("id"), -32602,
                            f"offset must be an integer, got {raw_offset!r}",
                        ),
                    )
                    return
                if offset_val < 0:
                    await messaging.publish(
                        reply,
                        build_rpc_error(
                            payload.get("id"), -32602,
                            f"offset must be non-negative, got {offset_val}",
                        ),
                    )
                    return

                if paged:
                    try:
                        requested_limit_int = int(requested_limit)
                    except (TypeError, ValueError):
                        await messaging.publish(
                            reply,
                            build_rpc_error(
                                payload.get("id"), -32602,
                                f"limit must be an integer, got {requested_limit!r}",
                            ),
                        )
                        return
                    # Reject ``limit <= 0`` rather than silently mapping
                    # it to the server cap. Mapping was surprising
                    # ("limit=0" usually means "no rows" elsewhere) and
                    # masked client bugs that passed unintentional zero
                    # / negative values.
                    if requested_limit_int <= 0:
                        await messaging.publish(
                            reply,
                            build_rpc_error(
                                payload.get("id"), -32602,
                                f"limit must be positive, got {requested_limit_int}",
                            ),
                        )
                        return
                    effective_limit = min(
                        requested_limit_int, _LIST_DEVICES_MAX_LIMIT,
                    )
                    # Surface silent clamps. The wire contract handles
                    # the over-cap request correctly via ``next_offset``
                    # — the caller just paginates more aggressively than
                    # it asked for — but without a log line operators
                    # tuning ``DEVICE_CONNECT_LIST_PAGE_SIZE`` above the
                    # server cap have no signal that their knob is being
                    # ignored. Deduped per process per requested value
                    # so a steady-state misconfigured client logs once,
                    # not once per page.
                    if (
                        effective_limit < requested_limit_int
                        and requested_limit_int not in _WARNED_LIMIT_CLAMPS
                    ):
                        _WARNED_LIMIT_CLAMPS.add(requested_limit_int)
                        logger.warning(
                            "discovery/listDevices: caller requested limit=%d, "
                            "clamped to server cap _LIST_DEVICES_MAX_LIMIT=%d. "
                            "Caller will paginate more aggressively than "
                            "expected. Raise DC_LIST_DEVICES_MAX_LIMIT (and "
                            "NATS max_payload) if the smaller page is "
                            "unintended.",
                            requested_limit_int, _LIST_DEVICES_MAX_LIMIT,
                        )
                    page, next_offset, total = await asyncio.to_thread(
                        registry.list_devices_page, tenant,
                        device_type=device_type,
                        location=location,
                        offset=offset_val,
                        limit=effective_limit,
                    )
                else:
                    page = await asyncio.to_thread(
                        registry.list_devices, tenant,
                        device_type=device_type, location=location,
                    )
                    # next_offset / total are unused on the legacy reply
                    # path (see the ``if paged`` branch below); the
                    # legacy shape is just ``{"devices": page}``.

                if acl_manager:
                    requester_id = params.get("requester_id", "")
                    # ACL filtering runs after pagination — devices the
                    # caller is not allowed to see are dropped from the
                    # page rather than from the unsliced fleet, so
                    # ``total_matched`` may be larger than what
                    # eventually reaches the requester and successive
                    # pages may be shorter than ``limit``. That's
                    # acceptable: ACL is opt-in and primarily a
                    # server-side hint, not a strict cardinality
                    # contract. Callers should not assume
                    # ``len(devices) == limit`` even mid-fleet.
                    page = acl_manager.filter_visible_devices(
                        requester_id, page, tenant=tenant
                    )

                if paged:
                    response_result = {
                        "devices": page,
                        "next_offset": next_offset,
                        "total_matched": total,
                    }
                else:
                    # Legacy reply shape: just ``devices``. Don't emit
                    # ``next_offset`` so old clients that ignore unknown
                    # keys aren't surprised by new metadata.
                    response_result = {"devices": page}
                await messaging.publish(
                    reply,
                    build_rpc_response(payload.get("id"), response_result),
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
                    build_rpc_response(payload.get("id"), {"device": device})
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
                    build_rpc_response(payload.get("id"), summary)
                )
            else:
                return  # Not a discovery method — ignore
        except Exception as e:
            await messaging.publish(
                reply,
                build_rpc_error(payload.get("id") if isinstance(payload, dict) else None, -32000, str(e))
            )

    return rpc_discovery


def _make_hb_handler(tenant: str, messaging=None):
    """Create a heartbeat handler bound to ``tenant``."""

    async def hb_handler(data_bytes: bytes, reply: Optional[str]):
        data: Any = None
        device_id: str = "?"
        try:
            data = json.loads(data_bytes)
            device_id = data.pop("device_id")
            # Update last_seen immediately (before blocking etcd calls)
            compound_key = f"{tenant}/{device_id}"
            _last_seen[compound_key] = time.time()
            # Pass TTL so refresh() can recover a lost lease after service restart
            ttl = _device_ttl.get(compound_key)
            await asyncio.to_thread(registry.refresh, tenant, device_id, ttl)
            data.pop("ts", None)  # transient heartbeat timestamp; tracked in _last_seen
            await asyncio.to_thread(registry.update_status, tenant, device_id, data)
            logger.debug("[device-registry] heartbeat from %s (tenant=%s)", device_id, tenant)

            # If the device has no lease (expired or lost after restart),
            # pull full registration info from the device and re-register it.
            if not registry.has_lease(tenant, device_id) and messaging is not None:
                logger.info(
                    "[device-registry] no lease for %s (tenant=%s), pulling registration",
                    device_id, tenant,
                )
                try:
                    cmd_subject = f"device-connect.{tenant}.{device_id}.cmd"
                    req = json.dumps({
                        "jsonrpc": "2.0",
                        "id": f"re-reg-{device_id}-{int(time.time()*1000)}",
                        "method": "requestRegistration",
                        "params": {},
                    }).encode()
                    resp_data = await messaging.request(
                        cmd_subject, req, timeout=_PULL_REGISTRATION_TIMEOUT,
                    )
                    resp = json.loads(resp_data)
                    result = resp.get("result", {})
                    # Validate through the same schema as registerDevice
                    params = RegisterParams(**result)
                    reg_payload = params.model_dump()
                    reg_ttl = params.device_ttl or _DEFAULT_TTL
                    await _do_register(tenant, device_id, reg_payload, reg_ttl, messaging)
                    logger.info(
                        "[device-registry] re-registered %s via pull (tenant=%s, ttl=%s)",
                        device_id, tenant, reg_ttl,
                    )
                except Exception as pull_err:
                    logger.warning(
                        "[device-registry] failed to pull registration from %s: %s",
                        device_id, pull_err,
                    )
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

    # Subscribe using wildcard subjects so new tenants work without restart.
    # The registry credentials are privileged (device-connect.>), so this is safe.
    # The tenant is extracted from the subject at runtime.

    # Cache per-tenant handlers to avoid re-creating closures on every message
    _register_handlers: dict[str, Any] = {}
    _discovery_handlers: dict[str, Any] = {}
    _heartbeat_handlers: dict[str, Any] = {}

    def _get_register_handler(tenant: str):
        if tenant not in _register_handlers:
            _register_handlers[tenant] = _make_register_handler(tenant, messaging)
        return _register_handlers[tenant]

    def _get_discovery_handler(tenant: str):
        if tenant not in _discovery_handlers:
            _discovery_handlers[tenant] = _make_list_handler(tenant, messaging, acl_manager)
        return _discovery_handlers[tenant]

    def _get_heartbeat_handler(tenant: str):
        if tenant not in _heartbeat_handlers:
            _heartbeat_handlers[tenant] = _make_hb_handler(tenant, messaging)
        return _heartbeat_handlers[tenant]

    # subscribe_with_subject passes (data, subject, reply) to the callback
    async def _wildcard_register(data: bytes, subject: str, reply):
        tenant = _extract_tenant(subject)
        await _get_register_handler(tenant)(data, reply)

    async def _wildcard_discovery(data: bytes, subject: str, reply):
        tenant = _extract_tenant(subject)
        await _get_discovery_handler(tenant)(data, reply)

    async def _wildcard_heartbeat(data: bytes, subject: str, reply):
        tenant = _extract_tenant(subject)
        await _get_heartbeat_handler(tenant)(data, reply)

    await messaging.subscribe_with_subject(
        "device-connect.*.registry",
        queue="registry",
        callback=_wildcard_register,
    )
    await messaging.subscribe_with_subject(
        "device-connect.*.discovery",
        queue="orch",
        callback=_wildcard_discovery,
    )
    await messaging.subscribe_with_subject(
        "device-connect.*.*.heartbeat",
        callback=_wildcard_heartbeat,
    )
    logger.info("[device-registry] listening on all tenants (wildcard)")

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

"""Zenoh helpers: RPC invocation and event streaming using ZenohAdapter."""

import json
import logging
import uuid
from pathlib import Path

from .. import config

logger = logging.getLogger(__name__)

# Registry credentials (privileged, can reach all tenants)
_REGISTRY_CREDS = Path(config.CREDS_DIR) / "registry.creds.json"


def _load_creds() -> dict:
    """Load registry credentials for Zenoh mTLS auth."""
    if _REGISTRY_CREDS.exists():
        with open(_REGISTRY_CREDS) as f:
            return json.load(f)
    return {}


async def connect():
    """Return a connected ZenohAdapter using registry credentials."""
    from device_connect_edge.messaging import ZenohAdapter

    adapter = ZenohAdapter()
    creds = _load_creds()
    zenoh_cfg = creds.get("zenoh", {})

    servers = zenoh_cfg.get("urls", [f"zenoh+tls://{config.ZENOH_HOST}:{config.ZENOH_PORT}"])
    tls = zenoh_cfg.get("tls", {})

    await adapter.connect(servers=servers, tls_config=tls if tls else None)
    return adapter


async def invoke(
    tenant: str, device_id: str, function: str,
    params: dict, timeout: float = 5.0,
) -> dict:
    """Send a JSON-RPC request to a device via Zenoh and return the response."""
    adapter = await connect()
    try:
        subject = f"device-connect.{tenant}.{device_id}.cmd"
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": function,
            "params": params,
        }

        result = await adapter.request(
            subject, json.dumps(payload).encode(), timeout=timeout,
        )
        return json.loads(result)
    except TimeoutError:
        return {"error": {"code": -2, "message": f"Request timed out after {timeout}s"}}
    except Exception as e:
        if "no respondent" in str(e).lower() or "timeout" in str(e).lower():
            return {"error": {"code": -1, "message": f"Device {device_id} is not responding"}}
        return {"error": {"code": -3, "message": str(e)}}
    finally:
        await adapter.close()

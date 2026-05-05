# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Agent API — JSON-only, Bearer-token authenticated namespace at /api/agent/v1/*.

Built for coding agents and CI clients. Distinct from the htmx browser views:

- always JSON; never HTML, never redirects
- per-token scopes (devices:read / :provision / :credentials / :invoke,
  events:read, admin:tenants / admin:*)
- read endpoints return whole sub-objects (status / identity / capabilities)
  so the API doesn't drop fields the registry adds later
- write endpoints return a stable {success, trace_id, result|error} envelope
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import uuid
from typing import Any

from aiohttp import web

from .. import config
from ..services import cli_auth as cli_auth_svc
from ..services import credentials as credentials_svc
from ..services import registry_client, tokens as tokens_svc
from ..services.backend import get_backend, validate_name

logger = logging.getLogger(__name__)
audit = logging.getLogger("device_connect.agent_api.audit")

PREFIX = "/api/agent/v1"

# Server-side hard caps for event streams. Apply regardless of what the client requests.
MAX_STREAM_DURATION_S = 3600       # 1 hour
MAX_STREAM_COUNT = 10_000


def setup_routes(app: web.Application):
    r = app.router
    # Public CLI-auth endpoints (exempt from Bearer middleware in app.py).
    r.add_post(PREFIX + "/auth/cli/init", auth_cli_init)
    r.add_post(PREFIX + "/auth/cli/poll", auth_cli_poll)

    r.add_get(PREFIX + "/me", me)
    r.add_get(PREFIX + "/fleet", fleet)
    r.add_get(PREFIX + "/devices", devices_list)
    r.add_post(PREFIX + "/devices", devices_provision)
    r.add_get(PREFIX + "/devices/{device_id}", device_get)
    r.add_delete(PREFIX + "/devices/{device_id}", device_delete)
    r.add_get(PREFIX + "/devices/{device_id}/identity", device_identity)
    r.add_get(PREFIX + "/devices/{device_id}/status", device_status)
    r.add_get(PREFIX + "/devices/{device_id}/capabilities", device_capabilities)
    r.add_get(PREFIX + "/devices/{device_id}/functions", device_functions)
    r.add_get(PREFIX + "/devices/{device_id}/events", device_events)
    r.add_get(PREFIX + "/devices/{device_id}/credentials", device_credentials_get)
    r.add_post(PREFIX + "/devices/{device_id}/credentials:rotate", device_credentials_rotate)
    r.add_post(PREFIX + "/devices/{device_id}/invoke", device_invoke)
    r.add_post(PREFIX + "/invoke-with-fallback", invoke_with_fallback)
    r.add_get(
        PREFIX + "/devices/{device_id}/events/{event_name}/stream",
        device_event_stream,
    )


# ── envelope helpers ────────────────────────────────────────────────


def _trace_id() -> str:
    return "trace-" + secrets.token_hex(8)


def _ok(result: Any, *, status: int = 200, trace_id: str | None = None) -> web.Response:
    return web.json_response(
        {"success": True, "trace_id": trace_id or _trace_id(), "result": result},
        status=status,
    )


def _err(
    *,
    status: int,
    code: str,
    message: str,
    trace_id: str | None = None,
) -> web.Response:
    return web.json_response(
        {"success": False, "trace_id": trace_id or _trace_id(),
         "error": {"code": code, "message": message}},
        status=status,
    )


def _require_scope(request: web.Request, scope: str) -> tuple[dict, web.Response | None]:
    record = request.get("token") or {}
    if not tokens_svc.has_scope(record, scope):
        return record, _err(status=403, code="missing_scope",
                            message=f"Token does not carry required scope: {scope}")
    return record, None


def _resolve_tenant(request: web.Request) -> tuple[str, web.Response | None]:
    """Resolve tenant: ?tenant= override requires admin role + admin:tenants/admin:*."""
    record = request.get("token") or {}
    user = request["user"]
    override = request.query.get("tenant")
    if override:
        if user.get("role") != "admin" or not (
            tokens_svc.has_scope(record, "admin:tenants") or tokens_svc.has_scope(record, "admin:*")
        ):
            return "", _err(status=403, code="tenant_override_forbidden",
                            message="tenant override requires admin role and admin scope")
        try:
            validate_name(override, "tenant")
        except ValueError as e:
            return "", _err(status=400, code="invalid_tenant", message=str(e))
        return override, None
    return user["tenant"], None


def _full_device_name(tenant: str, device_id: str) -> str:
    """Match the existing devices.create_device convention: tenant-prefixed, unless
    the caller already passed a fully-qualified id."""
    if device_id.startswith(f"{tenant}-"):
        return device_id
    return f"{tenant}-{device_id}"


def _audit(request: web.Request, action: str, **fields):
    record = request.get("token") or {}
    audit.info(
        "agent_api action=%s token_id=%s username=%s tenant=%s ip=%s ua=%s %s",
        action,
        record.get("token_id", "?"),
        record.get("username", "?"),
        record.get("tenant", "?"),
        request.remote or "?",
        request.headers.get("User-Agent", "?"),
        " ".join(f"{k}={v}" for k, v in fields.items()),
    )


# ── public CLI-auth init/poll ───────────────────────────────────────


def _verification_url(request: web.Request, request_id: str) -> str:
    scheme = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    return f"{scheme}://{host}/auth/cli/{request_id}"


async def auth_cli_init(request: web.Request) -> web.Response:
    """Public: start a CLI-auth flow. Returns a request_id and verification URL."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _err(status=400, code="invalid_json", message="Request body must be JSON")
    scopes = body.get("scopes") or []
    label = (body.get("label") or "").strip()[:64]
    if not isinstance(scopes, list) or not scopes:
        return _err(status=400, code="missing_scopes",
                    message="scopes must be a non-empty list")
    try:
        rec = cli_auth_svc.init(scopes_requested=list(scopes), label=label)
    except cli_auth_svc.CliAuthError as e:
        return _err(status=400, code="invalid_request", message=str(e))
    rec["verification_url"] = _verification_url(request, rec["request_id"])
    return web.json_response(rec)


async def auth_cli_poll(request: web.Request) -> web.Response:
    """Public: poll a pending CLI-auth flow.

    Returns:
      200 + token on approved (record consumed)
      202 on pending
      410 on denied / expired / not_found
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _err(status=400, code="invalid_json", message="Request body must be JSON")
    request_id = (body.get("request_id") or "").strip()
    if not request_id:
        return _err(status=400, code="missing_request_id", message="request_id is required")

    status, payload = cli_auth_svc.consume_on_poll(request_id)
    if status == "approved":
        return web.json_response({"status": "approved", **(payload or {})})
    if status == "pending":
        return web.json_response({"status": "pending"}, status=202)
    return web.json_response({"status": status}, status=410)


# ── /me, /fleet ─────────────────────────────────────────────────────


async def me(request: web.Request) -> web.Response:
    record = request.get("token") or {}
    return web.json_response({
        "username": record.get("username"),
        "tenant": record.get("tenant"),
        "role": record.get("role"),
        "scopes": record.get("scopes", []),
        "token_id": record.get("token_id"),
    })


async def fleet(request: web.Request) -> web.Response:
    _, err = _require_scope(request, "devices:read")
    if err:
        return err
    tenant, err = _resolve_tenant(request)
    if err:
        return err

    try:
        live = registry_client.list_live_devices(tenant)
    except Exception as e:
        logger.warning("fleet: registry query failed: %s", e)
        live = []

    creds = credentials_svc.list_credentials(tenant=tenant)
    by_type: dict[str, int] = {}
    for d in live:
        dt = d.get("device_type") or "unknown"
        by_type[dt] = by_type.get(dt, 0) + 1
    online = sum(1 for d in live if d.get("status") == "available")

    return web.json_response({
        "tenant": tenant,
        "devices_registered": len(live),
        "devices_online": online,
        "credentials_issued": len(creds),
        "by_device_type": by_type,
    })


# ── /devices read surface ───────────────────────────────────────────


def _paginate(items: list, request: web.Request) -> dict:
    try:
        offset = max(0, int(request.query.get("offset", "0")))
    except ValueError:
        offset = 0
    try:
        limit = int(request.query.get("limit", "200"))
    except ValueError:
        limit = 200
    limit = max(1, min(limit, 1000))

    page = items[offset: offset + limit]
    next_offset = offset + len(page) if (offset + len(page)) < len(items) else None
    return {
        "matched": len(items),
        "returned": len(page),
        "offset": offset,
        "next_offset": next_offset,
        "results": page,
    }


def _device_doc(tenant: str, device_id: str) -> dict | None:
    """Look up a device record. Tries the id as given, then with the tenant prefix."""
    doc = registry_client.get_device(tenant, device_id)
    if doc:
        return doc
    full = _full_device_name(tenant, device_id)
    if full != device_id:
        return registry_client.get_device(tenant, full)
    return None


async def devices_list(request: web.Request) -> web.Response:
    _, err = _require_scope(request, "devices:read")
    if err:
        return err
    tenant, err = _resolve_tenant(request)
    if err:
        return err

    try:
        live = registry_client.list_live_devices(tenant)
    except Exception as e:
        logger.warning("devices_list: registry query failed: %s", e)
        live = []

    results = [d.get("_raw", d) for d in live]
    return web.json_response(_paginate(results, request))


async def device_get(request: web.Request) -> web.Response:
    _, err = _require_scope(request, "devices:read")
    if err:
        return err
    tenant, err = _resolve_tenant(request)
    if err:
        return err

    device_id = request.match_info["device_id"]
    doc = _device_doc(tenant, device_id)
    if not doc:
        return _err(status=404, code="not_found", message=f"Device not found: {device_id}")
    return web.json_response(doc)


async def device_identity(request: web.Request) -> web.Response:
    _, err = _require_scope(request, "devices:read")
    if err:
        return err
    tenant, err = _resolve_tenant(request)
    if err:
        return err

    device_id = request.match_info["device_id"]
    doc = _device_doc(tenant, device_id)
    if not doc:
        return _err(status=404, code="not_found", message=f"Device not found: {device_id}")

    # Whole identity sub-object, verbatim — see plan: never project to a single field.
    return web.json_response({
        "device_id": doc.get("device_id", device_id),
        "identity": doc.get("identity") or {},
    })


async def device_status(request: web.Request) -> web.Response:
    _, err = _require_scope(request, "devices:read")
    if err:
        return err
    tenant, err = _resolve_tenant(request)
    if err:
        return err

    device_id = request.match_info["device_id"]
    doc = _device_doc(tenant, device_id)
    if not doc:
        return _err(status=404, code="not_found", message=f"Device not found: {device_id}")

    # Whole status sub-object, byte-equal with registry doc — agents must see every field.
    return web.json_response({
        "device_id": doc.get("device_id", device_id),
        "status": doc.get("status") or {},
    })


async def device_capabilities(request: web.Request) -> web.Response:
    _, err = _require_scope(request, "devices:read")
    if err:
        return err
    tenant, err = _resolve_tenant(request)
    if err:
        return err

    device_id = request.match_info["device_id"]
    doc = _device_doc(tenant, device_id)
    if not doc:
        return _err(status=404, code="not_found", message=f"Device not found: {device_id}")

    caps = doc.get("capabilities") or {}
    if not isinstance(caps, dict):
        # Some older registry rows used a list — coerce to the canonical shape.
        caps = {"description": "", "functions": [], "events": []}
    caps.setdefault("description", "")
    caps.setdefault("functions", [])
    caps.setdefault("events", [])

    return web.json_response({"device_id": doc.get("device_id", device_id), "capabilities": caps})


async def device_functions(request: web.Request) -> web.Response:
    """Convenience projection: byte-equal with capabilities.functions."""
    _, err = _require_scope(request, "devices:read")
    if err:
        return err
    tenant, err = _resolve_tenant(request)
    if err:
        return err

    device_id = request.match_info["device_id"]
    doc = _device_doc(tenant, device_id)
    if not doc:
        return _err(status=404, code="not_found", message=f"Device not found: {device_id}")
    caps = doc.get("capabilities") or {}
    funcs = caps.get("functions", []) if isinstance(caps, dict) else []
    return web.json_response({"device_id": doc.get("device_id", device_id), "functions": funcs})


async def device_events(request: web.Request) -> web.Response:
    """Convenience projection: byte-equal with capabilities.events."""
    _, err = _require_scope(request, "devices:read")
    if err:
        return err
    tenant, err = _resolve_tenant(request)
    if err:
        return err

    device_id = request.match_info["device_id"]
    doc = _device_doc(tenant, device_id)
    if not doc:
        return _err(status=404, code="not_found", message=f"Device not found: {device_id}")
    caps = doc.get("capabilities") or {}
    events = caps.get("events", []) if isinstance(caps, dict) else []
    return web.json_response({"device_id": doc.get("device_id", device_id), "events": events})


# ── provisioning ────────────────────────────────────────────────────


async def devices_provision(request: web.Request) -> web.Response:
    """Create a new device + return its credentials inline in one call.

    Body: {"device_name": str, "device_type"?, "location"?, "description"?, "metadata"?}
    Returns 201 with {device, credentials: {filename, content}}.
    """
    trace = _trace_id()
    _, err = _require_scope(request, "devices:provision")
    if err:
        return err
    tenant, err = _resolve_tenant(request)
    if err:
        return err

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _err(status=400, code="invalid_json", message="Request body must be JSON",
                    trace_id=trace)

    device_name = (body.get("device_name") or "").strip()
    if not device_name:
        return _err(status=400, code="missing_device_name",
                    message="device_name is required", trace_id=trace)

    full_name = _full_device_name(tenant, device_name)
    try:
        validate_name(full_name, "device name")
    except ValueError as e:
        return _err(status=400, code="invalid_device_name", message=str(e), trace_id=trace)

    backend = get_backend()
    if not backend.is_bootstrapped():
        return _err(status=503, code="not_bootstrapped",
                    message="System not bootstrapped — admin must run setup first",
                    trace_id=trace)

    try:
        broker_info = backend.broker_display_info()
        await backend.add_device(
            tenant, full_name,
            host=broker_info["host"], port=broker_info["port"],
        )
        await backend.reload_broker()
    except Exception as e:
        logger.exception("provision failed for %s/%s", tenant, full_name)
        return _err(status=500, code="provision_failed",
                    message=f"Failed to create device: {e}", trace_id=trace)

    filename = f"{full_name}.creds.json"
    cred_data = credentials_svc.get_credential_data(filename) or {}

    device_record = {
        "device_id": full_name,
        "tenant": tenant,
        "identity": {
            "device_type": body.get("device_type") or "",
            "description": body.get("description") or "",
        },
        "status": {"location": body.get("location") or "", "availability": "unknown"},
        "metadata": body.get("metadata") or {},
    }

    _audit(request, "provision", trace_id=trace, device_id=full_name)
    return _ok(
        {"device": device_record,
         "credentials": {"filename": filename, "content": cred_data}},
        status=201,
        trace_id=trace,
    )


async def device_credentials_get(request: web.Request) -> web.Response:
    """Re-download a device's credential file as inline JSON."""
    trace = _trace_id()
    _, err = _require_scope(request, "devices:credentials")
    if err:
        return err
    tenant, err = _resolve_tenant(request)
    if err:
        return err

    device_id = request.match_info["device_id"]
    full_name = _full_device_name(tenant, device_id)
    filename = f"{full_name}.creds.json"
    cred = credentials_svc.get_credential_data(filename)
    if not cred:
        # Try the id as given (admin-style absolute lookup)
        cred = credentials_svc.get_credential_data(f"{device_id}.creds.json")
        if cred:
            full_name = device_id
            filename = f"{device_id}.creds.json"
    if not cred:
        return _err(status=404, code="not_found",
                    message=f"Credential file not found for {device_id}", trace_id=trace)

    # Tenant binding check (admins with admin scope have already been allowed via override)
    record = request.get("token") or {}
    user = request["user"]
    if cred.get("tenant") and cred.get("tenant") != user["tenant"]:
        if user.get("role") != "admin" or not (
            tokens_svc.has_scope(record, "admin:tenants") or tokens_svc.has_scope(record, "admin:*")
        ):
            return _err(status=403, code="tenant_mismatch",
                        message="Credential belongs to another tenant", trace_id=trace)

    _audit(request, "credentials_get", trace_id=trace, device_id=full_name)
    return _ok({"filename": filename, "content": cred}, trace_id=trace)


async def device_credentials_rotate(request: web.Request) -> web.Response:
    """Rotate a device's credentials. Implementation defers to the active backend.

    Phase-2 scope is to expose the endpoint with strong audit + scope enforcement.
    Backends that don't yet implement rotation surface a clear 501.
    """
    trace = _trace_id()
    _, err = _require_scope(request, "devices:credentials")
    if err:
        return err
    tenant, err = _resolve_tenant(request)
    if err:
        return err

    device_id = request.match_info["device_id"]
    full_name = _full_device_name(tenant, device_id)

    backend = get_backend()
    rotate = getattr(backend, "rotate_device_credentials", None)
    if rotate is None:
        return _err(status=501, code="not_implemented",
                    message=f"Credential rotation not yet supported on the {backend.backend_name()} backend",
                    trace_id=trace)

    try:
        await rotate(tenant, full_name)
    except Exception as e:
        logger.exception("rotate failed for %s/%s", tenant, full_name)
        return _err(status=500, code="rotate_failed", message=str(e), trace_id=trace)

    filename = f"{full_name}.creds.json"
    cred_data = credentials_svc.get_credential_data(filename) or {}
    _audit(request, "credentials_rotate", trace_id=trace, device_id=full_name)
    return _ok({"filename": filename, "content": cred_data}, trace_id=trace)


async def device_delete(request: web.Request) -> web.Response:
    """Decommission a device. Requires devices:provision."""
    trace = _trace_id()
    _, err = _require_scope(request, "devices:provision")
    if err:
        return err
    tenant, err = _resolve_tenant(request)
    if err:
        return err

    device_id = request.match_info["device_id"]
    full_name = _full_device_name(tenant, device_id)

    backend = get_backend()
    remove = getattr(backend, "remove_device", None)
    if remove is None:
        return _err(status=501, code="not_implemented",
                    message=f"Device removal not yet supported on the {backend.backend_name()} backend",
                    trace_id=trace)
    try:
        await remove(tenant, full_name)
    except Exception as e:
        logger.exception("delete failed for %s/%s", tenant, full_name)
        return _err(status=500, code="delete_failed", message=str(e), trace_id=trace)

    _audit(request, "delete", trace_id=trace, device_id=full_name)
    return _ok({"device_id": full_name, "deleted": True}, trace_id=trace)


# ── invocation ──────────────────────────────────────────────────────


def _truncate(s: str, n: int = 200) -> str:
    if not isinstance(s, str):
        return ""
    return s if len(s) <= n else s[: n - 1] + "…"


async def device_invoke(request: web.Request) -> web.Response:
    """Invoke an RPC on a single device.

    Body: {"function": str, "params"?: dict, "reason"?: str, "timeout"?: float}
    """
    trace = _trace_id()
    _, err = _require_scope(request, "devices:invoke")
    if err:
        return err
    tenant, err = _resolve_tenant(request)
    if err:
        return err

    device_id = request.match_info["device_id"]
    full_name = _full_device_name(tenant, device_id)

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _err(status=400, code="invalid_json", message="Request body must be JSON",
                    trace_id=trace)

    function = (body.get("function") or "").strip()
    if not function:
        return _err(status=400, code="missing_function", message="function is required",
                    trace_id=trace)

    params = body.get("params") or {}
    if not isinstance(params, dict):
        return _err(status=400, code="invalid_params", message="params must be an object",
                    trace_id=trace)
    timeout = float(body.get("timeout") or 5.0)
    reason = _truncate(body.get("reason") or body.get("llm_reasoning") or "", 500)

    backend = get_backend()
    started = time.monotonic()
    try:
        result = await backend.rpc_invoke(tenant, full_name, function, params, timeout=timeout)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        _audit(request, "invoke", trace_id=trace, device_id=full_name,
               function=function, elapsed_ms=elapsed_ms, success=True,
               reason=_truncate(reason, 120))
        return _ok({"device_id": full_name, "function": function,
                    "elapsed_ms": elapsed_ms, "response": result},
                   trace_id=trace)
    except Exception as e:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        _audit(request, "invoke", trace_id=trace, device_id=full_name,
               function=function, elapsed_ms=elapsed_ms, success=False,
               reason=_truncate(reason, 120), error=str(e))
        return _err(status=502, code="invoke_failed", message=str(e), trace_id=trace)


async def invoke_with_fallback(request: web.Request) -> web.Response:
    """Try a list of devices in order; return the first success + per-device failures.

    Body: {"device_ids": [str, ...], "function": str, "params"?, "reason"?, "timeout"?}
    """
    trace = _trace_id()
    _, err = _require_scope(request, "devices:invoke")
    if err:
        return err
    tenant, err = _resolve_tenant(request)
    if err:
        return err

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _err(status=400, code="invalid_json", message="Request body must be JSON",
                    trace_id=trace)

    ids = body.get("device_ids") or []
    if not isinstance(ids, list) or not ids:
        return _err(status=400, code="missing_device_ids",
                    message="device_ids must be a non-empty list", trace_id=trace)
    function = (body.get("function") or "").strip()
    if not function:
        return _err(status=400, code="missing_function", message="function is required",
                    trace_id=trace)
    params = body.get("params") or {}
    timeout = float(body.get("timeout") or 5.0)
    reason = _truncate(body.get("reason") or body.get("llm_reasoning") or "", 500)

    backend = get_backend()
    failures = []
    for raw_id in ids:
        full_name = _full_device_name(tenant, raw_id)
        started = time.monotonic()
        try:
            response = await backend.rpc_invoke(tenant, full_name, function, params, timeout=timeout)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            _audit(request, "invoke_fallback", trace_id=trace, device_id=full_name,
                   function=function, elapsed_ms=elapsed_ms, success=True,
                   reason=_truncate(reason, 120))
            return _ok(
                {"device_id": full_name, "function": function,
                 "elapsed_ms": elapsed_ms, "response": response,
                 "tried": [{"device_id": _full_device_name(tenant, x), "ok": (x == raw_id)}
                           for x in ids[: ids.index(raw_id) + 1]],
                 "failures": failures},
                trace_id=trace,
            )
        except Exception as e:
            failures.append({"device_id": full_name, "error": str(e)})

    _audit(request, "invoke_fallback", trace_id=trace, function=function, success=False,
           reason=_truncate(reason, 120))
    return _err(status=502, code="all_failed",
                message="All fallback devices failed", trace_id=trace)


# ── event streaming (bounded) ──────────────────────────────────────


def _parse_bool(v: str | None) -> bool:
    return (v or "").lower() in ("1", "true", "yes", "y")


async def device_event_stream(request: web.Request) -> web.Response:
    """Bounded event stream.

    Query params:
      format:   "ndjson" (default) | "sse"
      duration: max wall-clock seconds before close (>=1, server caps at MAX_STREAM_DURATION_S)
      count:    max events delivered before close (>=1, server caps at MAX_STREAM_COUNT)
      follow:   true to allow unbounded; at least one of duration/count/follow MUST be set

    NDJSON output emits a final "_meta" line with closed_by/events_received/elapsed_s.
    """
    _, err = _require_scope(request, "events:read")
    if err:
        return err
    tenant, err = _resolve_tenant(request)
    if err:
        return err

    device_id = request.match_info["device_id"]
    event_name = request.match_info["event_name"]
    full_name = _full_device_name(tenant, device_id)

    fmt = (request.query.get("format") or "ndjson").lower()
    if fmt not in ("ndjson", "sse"):
        return _err(status=400, code="invalid_format",
                    message="format must be 'ndjson' or 'sse'")

    follow = _parse_bool(request.query.get("follow"))
    duration_raw = request.query.get("duration")
    count_raw = request.query.get("count")

    if duration_raw is None and count_raw is None and not follow:
        return _err(status=400, code="missing_bound",
                    message="at least one of duration, count, or follow must be supplied")

    try:
        duration = float(duration_raw) if duration_raw is not None else None
        count = int(count_raw) if count_raw is not None else None
    except ValueError:
        return _err(status=400, code="invalid_bound",
                    message="duration must be a number; count must be an integer")

    if duration is not None:
        if duration <= 0:
            return _err(status=400, code="invalid_bound", message="duration must be > 0")
        duration = min(duration, float(MAX_STREAM_DURATION_S))
    if count is not None:
        if count <= 0:
            return _err(status=400, code="invalid_bound", message="count must be > 0")
        count = min(count, MAX_STREAM_COUNT)
    # Server hard cap even on follow=true:
    if duration is None and count is None:
        duration = float(MAX_STREAM_DURATION_S)
        count = MAX_STREAM_COUNT

    headers = {"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}
    if fmt == "sse":
        headers["Content-Type"] = "text/event-stream"
    else:
        headers["Content-Type"] = "application/x-ndjson"

    response = web.StreamResponse(headers=headers)
    await response.prepare(request)

    backend = get_backend()
    client = None
    sub = None
    started = time.monotonic()
    received = 0
    closed_by = "server"

    try:
        client = await backend.rpc_connect()
        subject = f"device-connect.{tenant}.{full_name}.event.{event_name}"
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)

        async def _on_msg_generic(msg_data, _subject=None):
            await queue.put(msg_data)

        if backend.backend_name() == "nats":
            async def _nats_cb(msg):
                await queue.put(msg.data)
            sub = await backend.subscribe_events(client, subject, _nats_cb)
        else:
            sub = await backend.subscribe_events(client, subject, _on_msg_generic)

        if fmt == "sse":
            await response.write(b": connected\n\n")

        while True:
            elapsed = time.monotonic() - started
            if duration is not None and elapsed >= duration:
                closed_by = "duration"
                break
            if count is not None and received >= count:
                closed_by = "count"
                break

            wait_for = 15.0
            if duration is not None:
                wait_for = max(0.1, min(wait_for, duration - elapsed))

            try:
                raw = await asyncio.wait_for(queue.get(), timeout=wait_for)
            except asyncio.TimeoutError:
                if fmt == "sse":
                    await response.write(b": keepalive\n\n")
                continue

            try:
                payload = json.loads(raw if isinstance(raw, (str, bytes)) else raw)
            except (json.JSONDecodeError, TypeError):
                payload = {"raw": raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)}

            event = {
                "device_id": full_name,
                "event": event_name,
                "ts": time.time(),
                "params": payload.get("params", payload),
            }
            received += 1
            if fmt == "sse":
                await response.write(f"data: {json.dumps(event)}\n\n".encode())
            else:
                await response.write((json.dumps(event) + "\n").encode())
    except (ConnectionResetError, asyncio.CancelledError):
        closed_by = "client_disconnect"
    finally:
        try:
            if client is not None:
                await backend.unsubscribe_events(client, sub)
        except Exception:
            logger.debug("unsubscribe_events failed", exc_info=True)
        elapsed = time.monotonic() - started
        if fmt == "ndjson":
            try:
                trailer = {"_meta": {"closed_by": closed_by,
                                     "events_received": received,
                                     "elapsed_s": round(elapsed, 3)}}
                await response.write((json.dumps(trailer) + "\n").encode())
            except (ConnectionResetError, asyncio.CancelledError):
                pass

    return response

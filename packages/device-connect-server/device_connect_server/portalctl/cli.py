# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""dc-portalctl — CLI for the Device Connect Portal agent API.

Designed for coding agents and CI: JSON output by default, exit codes that
distinguish auth/scope/server errors, and bounded event streaming that never
hangs by accident.

Configuration:
    DEVICE_CONNECT_PORTAL_URL    base URL (default: http://localhost:8080)
    DEVICE_CONNECT_PORTAL_TOKEN  Bearer token (dcp_...)

See `dc-portalctl --help` for the full command list.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

def _require_aiohttp():
    try:
        import aiohttp  # noqa: F401
        return aiohttp
    except ImportError as e:
        sys.stderr.write("dc-portalctl requires aiohttp (install device-connect-server[portal])\n")
        raise SystemExit(1) from e


DEFAULT_URL = "http://localhost:8080"
ENV_URL = "DEVICE_CONNECT_PORTAL_URL"
ENV_TOKEN = "DEVICE_CONNECT_PORTAL_TOKEN"


# ── output helpers ────────────────────────────────────────────────


def _emit_json(value: Any, fp=None) -> None:
    fp = fp or sys.stdout
    json.dump(value, fp, indent=2, sort_keys=False, default=str)
    fp.write("\n")
    fp.flush()


def _emit_compact(value: Any) -> None:
    """Compact human-readable view for read-only commands."""
    if isinstance(value, list):
        for item in value:
            _emit_compact(item)
        return
    if isinstance(value, dict):
        if "device_id" in value:
            line = value["device_id"]
            if "device_type" in value or "identity" in value:
                ident = value.get("identity") or {}
                dtype = value.get("device_type") or ident.get("device_type") or ""
                if dtype:
                    line += f"\t{dtype}"
            status = value.get("status") or {}
            if isinstance(status, dict):
                avail = status.get("availability")
                if avail:
                    line += f"\t{avail}"
            print(line)
            return
    print(value)


def _emit(value: Any, fmt: str) -> None:
    if fmt == "json":
        _emit_json(value)
    elif fmt == "compact":
        _emit_compact(value)
    elif fmt == "table":
        _emit_compact(value)
    else:
        _emit_json(value)


# ── HTTP client ───────────────────────────────────────────────────


class PortalClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    async def request(self, method: str, path: str, *, json_body: dict | None = None,
                      params: dict | None = None) -> tuple[int, Any]:
        aiohttp = _require_aiohttp()
        url = self.base_url + path
        async with aiohttp.ClientSession() as session:
            async with session.request(method, url, headers=self.headers,
                                       json=json_body, params=params) as resp:
                text = await resp.text()
                try:
                    body = json.loads(text) if text else {}
                except json.JSONDecodeError:
                    body = {"raw": text}
                return resp.status, body


def _resolve_config(args) -> tuple[str, str]:
    url = args.portal_url or os.environ.get(ENV_URL) or DEFAULT_URL
    token = args.token or os.environ.get(ENV_TOKEN) or ""
    if not token:
        sys.stderr.write(
            f"error: no portal token provided. Set {ENV_TOKEN}=dcp_... or pass --token.\n"
        )
        raise SystemExit(2)
    return url, token


def _exit_for_status(status: int, body: Any) -> int:
    """Map HTTP status to an exit code agents can branch on."""
    if 200 <= status < 300:
        return 0
    if status == 401:
        return 4
    if status == 403:
        return 5
    if status == 404:
        return 6
    return 1


def _maybe_print_error(status: int, body: Any):
    if 200 <= status < 300:
        return
    sys.stderr.write(f"HTTP {status}: ")
    if isinstance(body, dict) and body.get("error"):
        err = body["error"]
        sys.stderr.write(f"{err.get('code', '?')}: {err.get('message', '')}\n")
    else:
        sys.stderr.write(json.dumps(body) + "\n")


# ── commands ──────────────────────────────────────────────────────


async def cmd_auth_me(client: PortalClient, args) -> int:
    status, body = await client.request("GET", "/api/agent/v1/me")
    _maybe_print_error(status, body)
    if 200 <= status < 300:
        _emit(body, args.output)
    return _exit_for_status(status, body)


async def cmd_fleet(client: PortalClient, args) -> int:
    params = {"tenant": args.tenant} if args.tenant else None
    status, body = await client.request("GET", "/api/agent/v1/fleet", params=params)
    _maybe_print_error(status, body)
    if 200 <= status < 300:
        _emit(body, args.output)
    return _exit_for_status(status, body)


async def cmd_devices_list(client: PortalClient, args) -> int:
    params: dict[str, Any] = {"offset": args.offset, "limit": args.limit}
    if args.tenant:
        params["tenant"] = args.tenant
    status, body = await client.request("GET", "/api/agent/v1/devices", params=params)
    _maybe_print_error(status, body)
    if 200 <= status < 300:
        _emit(body, args.output)
    return _exit_for_status(status, body)


def _device_subpath(device_id: str, sub: str) -> str:
    return f"/api/agent/v1/devices/{device_id}{sub}"


async def cmd_devices_get(client: PortalClient, args) -> int:
    return await _simple_get(client, args, _device_subpath(args.device_id, ""))


async def cmd_devices_identity(client: PortalClient, args) -> int:
    return await _simple_get(client, args, _device_subpath(args.device_id, "/identity"))


async def cmd_devices_status(client: PortalClient, args) -> int:
    return await _simple_get(client, args, _device_subpath(args.device_id, "/status"))


async def cmd_devices_capabilities(client: PortalClient, args) -> int:
    return await _simple_get(client, args, _device_subpath(args.device_id, "/capabilities"))


async def cmd_devices_functions(client: PortalClient, args) -> int:
    return await _simple_get(client, args, _device_subpath(args.device_id, "/functions"))


async def cmd_devices_events(client: PortalClient, args) -> int:
    return await _simple_get(client, args, _device_subpath(args.device_id, "/events"))


async def _simple_get(client: PortalClient, args, path: str) -> int:
    params = {"tenant": args.tenant} if getattr(args, "tenant", None) else None
    status, body = await client.request("GET", path, params=params)
    _maybe_print_error(status, body)
    if 200 <= status < 300:
        _emit(body, args.output)
    return _exit_for_status(status, body)


async def cmd_devices_provision(client: PortalClient, args) -> int:
    body_in = {"device_name": args.device_name}
    if args.device_type:
        body_in["device_type"] = args.device_type
    if args.location:
        body_in["location"] = args.location
    if args.description:
        body_in["description"] = args.description
    if args.metadata:
        meta = {}
        for kv in args.metadata:
            if "=" not in kv:
                sys.stderr.write(f"--metadata expects key=value, got: {kv}\n")
                return 2
            k, v = kv.split("=", 1)
            meta[k] = v
        body_in["metadata"] = meta

    params = {"tenant": args.tenant} if args.tenant else None
    status, body = await client.request("POST", "/api/agent/v1/devices",
                                        json_body=body_in, params=params)
    _maybe_print_error(status, body)
    if not (200 <= status < 300):
        return _exit_for_status(status, body)

    result = (body or {}).get("result") or {}
    creds = result.get("credentials") or {}
    if args.creds_output_file:
        try:
            with open(args.creds_output_file, "w") as fp:
                json.dump(creds.get("content") or {}, fp, indent=2)
                fp.write("\n")
            sys.stderr.write(f"Wrote credentials to {args.creds_output_file}\n")
        except OSError as e:
            sys.stderr.write(f"failed to write credentials file: {e}\n")
            return 1
    _emit(body, args.output)
    return 0


async def cmd_devices_credentials(client: PortalClient, args) -> int:
    params = {"tenant": args.tenant} if args.tenant else None
    status, body = await client.request(
        "GET", _device_subpath(args.device_id, "/credentials"), params=params,
    )
    _maybe_print_error(status, body)
    if not (200 <= status < 300):
        return _exit_for_status(status, body)
    result = (body or {}).get("result") or {}
    if args.output_file:
        try:
            with open(args.output_file, "w") as fp:
                json.dump(result.get("content") or {}, fp, indent=2)
                fp.write("\n")
            sys.stderr.write(f"Wrote credentials to {args.output_file}\n")
        except OSError as e:
            sys.stderr.write(f"failed to write credentials file: {e}\n")
            return 1
        return 0
    _emit(body, args.output)
    return 0


async def cmd_devices_revoke_credentials(client: PortalClient, args) -> int:
    params = {"tenant": args.tenant} if args.tenant else None
    status, body = await client.request(
        "POST", _device_subpath(args.device_id, "/credentials:rotate"), params=params,
    )
    _maybe_print_error(status, body)
    if 200 <= status < 300:
        _emit(body, args.output)
    return _exit_for_status(status, body)


async def cmd_devices_delete(client: PortalClient, args) -> int:
    if not args.confirm:
        sys.stderr.write("delete requires --confirm to proceed\n")
        return 2
    params = {"tenant": args.tenant} if args.tenant else None
    status, body = await client.request(
        "DELETE", _device_subpath(args.device_id, ""), params=params,
    )
    _maybe_print_error(status, body)
    if 200 <= status < 300:
        _emit(body, args.output)
    return _exit_for_status(status, body)


async def cmd_devices_invoke(client: PortalClient, args) -> int:
    try:
        params_obj = json.loads(args.params) if args.params else {}
    except json.JSONDecodeError as e:
        sys.stderr.write(f"--params must be valid JSON: {e}\n")
        return 2
    body_in = {
        "function": args.function,
        "params": params_obj,
        "timeout": args.timeout,
    }
    if args.reason:
        body_in["reason"] = args.reason
    qs = {"tenant": args.tenant} if args.tenant else None
    status, body = await client.request(
        "POST", _device_subpath(args.device_id, "/invoke"),
        json_body=body_in, params=qs,
    )
    _maybe_print_error(status, body)
    if 200 <= status < 300:
        _emit(body, args.output)
    return _exit_for_status(status, body)


async def cmd_devices_invoke_fallback(client: PortalClient, args) -> int:
    try:
        params_obj = json.loads(args.params) if args.params else {}
    except json.JSONDecodeError as e:
        sys.stderr.write(f"--params must be valid JSON: {e}\n")
        return 2
    ids = [s.strip() for s in args.device_ids.split(",") if s.strip()]
    if not ids:
        sys.stderr.write("no device ids provided\n")
        return 2
    body_in = {
        "device_ids": ids,
        "function": args.function,
        "params": params_obj,
        "timeout": args.timeout,
    }
    if args.reason:
        body_in["reason"] = args.reason
    qs = {"tenant": args.tenant} if args.tenant else None
    status, body = await client.request(
        "POST", "/api/agent/v1/invoke-with-fallback", json_body=body_in, params=qs,
    )
    _maybe_print_error(status, body)
    if 200 <= status < 300:
        _emit(body, args.output)
    return _exit_for_status(status, body)


async def cmd_devices_stream(client: PortalClient, args) -> int:
    if args.duration is None and args.count is None and not args.follow:
        sys.stderr.write(
            "error: stream requires at least one of --duration, --count, or --follow\n"
        )
        return 2

    aiohttp = _require_aiohttp()
    from .streaming import stream_ndjson

    qs: dict[str, Any] = {"format": args.format}
    if args.duration is not None:
        qs["duration"] = args.duration
    if args.count is not None:
        qs["count"] = args.count
    if args.follow:
        qs["follow"] = "true"
    if args.tenant:
        qs["tenant"] = args.tenant

    url = client.base_url + _device_subpath(args.device_id, f"/events/{args.event_name}/stream")
    async with aiohttp.ClientSession() as session:
        # Build query string ourselves to keep it readable in error messages
        from urllib.parse import urlencode
        full_url = url + "?" + urlencode(qs)
        if args.format == "ndjson":
            exit_code, _events, _closed_by = await stream_ndjson(
                session, full_url, client.headers,
                duration=args.duration, count=args.count, follow=args.follow,
            )
            return exit_code
        # SSE: pass through with no client-side _meta trailer
        async with session.get(full_url, headers=client.headers) as resp:
            if resp.status >= 400:
                txt = await resp.text()
                sys.stderr.write(f"stream failed: HTTP {resp.status}: {txt}\n")
                return _exit_for_status(resp.status, {})
            async for raw in resp.content:
                sys.stdout.write(raw.decode(errors="replace"))
                sys.stdout.flush()
            return 0


# ── argparse wiring ────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dc-portalctl",
        description="CLI client for the Device Connect Portal agent API",
    )
    p.add_argument("--portal-url", help=f"portal base URL (default ${ENV_URL} or {DEFAULT_URL})")
    p.add_argument("--token", help=f"bearer token (default ${ENV_TOKEN})")
    p.add_argument("--tenant", help="tenant override (admin only)")
    p.add_argument("--output", choices=["json", "table", "compact"], default="json",
                   help="output format (default: json)")

    sub = p.add_subparsers(dest="cmd", required=True)

    # auth me
    auth = sub.add_parser("auth", help="authentication / identity")
    auth_sub = auth.add_subparsers(dest="auth_cmd", required=True)
    auth_sub.add_parser("me", help="show identity + scopes for the current token") \
        .set_defaults(func=cmd_auth_me)

    # fleet describe
    fleet = sub.add_parser("fleet", help="fleet-level queries")
    fsub = fleet.add_subparsers(dest="fleet_cmd", required=True)
    fsub.add_parser("describe", help="fleet summary").set_defaults(func=cmd_fleet)

    # devices ...
    devices = sub.add_parser("devices", help="device operations")
    dsub = devices.add_subparsers(dest="devices_cmd", required=True)

    d_list = dsub.add_parser("list", help="list devices")
    d_list.add_argument("--offset", type=int, default=0)
    d_list.add_argument("--limit", type=int, default=200)
    d_list.set_defaults(func=cmd_devices_list)

    for name, fn, help_ in [
        ("get", cmd_devices_get, "whole device record"),
        ("identity", cmd_devices_identity, "identity sub-object"),
        ("status", cmd_devices_status, "entire status sub-object"),
        ("capabilities", cmd_devices_capabilities, "{functions, events}"),
        ("functions", cmd_devices_functions, "device functions"),
        ("events", cmd_devices_events, "device events"),
    ]:
        sp = dsub.add_parser(name, help=help_)
        sp.add_argument("device_id")
        sp.set_defaults(func=fn)

    d_prov = dsub.add_parser("provision", help="create a new device + return credentials")
    d_prov.add_argument("device_name")
    d_prov.add_argument("--device-type")
    d_prov.add_argument("--location")
    d_prov.add_argument("--description")
    d_prov.add_argument("--metadata", action="append", default=[],
                        help="key=value (repeatable)")
    d_prov.add_argument("--creds-output-file",
                        help="write credentials JSON to this path")
    d_prov.set_defaults(func=cmd_devices_provision)

    d_creds = dsub.add_parser("credentials", help="re-download credentials for a device")
    d_creds.add_argument("device_id")
    d_creds.add_argument("--output-file", help="write credentials JSON to this path")
    d_creds.set_defaults(func=cmd_devices_credentials)

    d_rev = dsub.add_parser("revoke-credentials", help="rotate / invalidate credentials")
    d_rev.add_argument("device_id")
    d_rev.set_defaults(func=cmd_devices_revoke_credentials)

    d_del = dsub.add_parser("delete", help="decommission a device")
    d_del.add_argument("device_id")
    d_del.add_argument("--confirm", action="store_true",
                       help="required: confirms the destructive action")
    d_del.set_defaults(func=cmd_devices_delete)

    d_inv = dsub.add_parser("invoke", help="invoke a device function")
    d_inv.add_argument("device_id")
    d_inv.add_argument("function")
    d_inv.add_argument("--params", default="{}", help="JSON params object")
    d_inv.add_argument("--reason", help="agent reasoning (audited, truncated)")
    d_inv.add_argument("--timeout", type=float, default=10.0)
    d_inv.set_defaults(func=cmd_devices_invoke)

    d_invf = dsub.add_parser("invoke-fallback",
                             help="try a comma-separated device list in order")
    d_invf.add_argument("device_ids", help="comma-separated list")
    d_invf.add_argument("function")
    d_invf.add_argument("--params", default="{}")
    d_invf.add_argument("--reason")
    d_invf.add_argument("--timeout", type=float, default=10.0)
    d_invf.set_defaults(func=cmd_devices_invoke_fallback)

    d_str = dsub.add_parser("stream", help="bounded event stream for a device")
    d_str.add_argument("device_id")
    d_str.add_argument("event_name")
    d_str.add_argument("--format", choices=["ndjson", "sse"], default="ndjson")
    d_str.add_argument("--duration", type=float,
                       help="max wall-clock seconds before the stream closes")
    d_str.add_argument("--count", type=int,
                       help="max events delivered before the stream closes")
    d_str.add_argument("--follow", action="store_true",
                       help="explicit unbounded mode (still capped server-side)")
    d_str.set_defaults(func=cmd_devices_stream)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    url, token = _resolve_config(args)
    client = PortalClient(url, token)
    try:
        return asyncio.run(args.func(client, args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

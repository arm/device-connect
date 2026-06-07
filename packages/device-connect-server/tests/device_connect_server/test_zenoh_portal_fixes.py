# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for the Zenoh DC-Portal enablement fixes.

- zenoh_rpc.connect() must obtain a client via the messaging factory rather
  than importing ZenohAdapter from the package root (which does not export it).
- the invoke client must be cached and reused across calls, and dropped on an
  unknown transport failure.
- run_verification() must read the ACL from the top-level "access_control"
  config key, not from "plugins.access_control".
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from device_connect_server.portal.services import zenoh_rpc


@pytest.fixture(autouse=True)
def _reset_cached_client():
    """Each test starts with no cached invoke client."""
    zenoh_rpc._invoke_client = None
    yield
    zenoh_rpc._invoke_client = None


@pytest.mark.asyncio
async def test_connect_uses_messaging_factory(monkeypatch):
    """connect() must call create_client('zenoh'), not the missing root import."""
    adapter = MagicMock()
    adapter.connect = AsyncMock()
    factory = MagicMock(return_value=adapter)

    # The import lives inside connect(); patch it at the source module.
    import device_connect_edge.messaging as messaging
    monkeypatch.setattr(messaging, "create_client", factory, raising=True)
    monkeypatch.setattr(zenoh_rpc, "_load_creds", lambda: {"zenoh": {"urls": ["tls/r:7447"]}})

    result = await zenoh_rpc.connect()

    factory.assert_called_once_with("zenoh")
    adapter.connect.assert_awaited_once()
    assert result is adapter


@pytest.mark.asyncio
async def test_invoke_client_is_cached(monkeypatch):
    """Two invokes reuse a single adapter instead of reconnecting each time."""
    adapter = MagicMock()
    adapter.is_closed = False
    adapter.request = AsyncMock(return_value=b'{"result": {"ok": true}}')
    connect = AsyncMock(return_value=adapter)
    monkeypatch.setattr(zenoh_rpc, "connect", connect)

    r1 = await zenoh_rpc.invoke("t", "dev", "fn", {})
    r2 = await zenoh_rpc.invoke("t", "dev", "fn", {})

    assert r1 == {"result": {"ok": True}}
    assert r2 == {"result": {"ok": True}}
    connect.assert_awaited_once()  # cached -- only one connect for two invokes
    assert adapter.request.await_count == 2


@pytest.mark.asyncio
async def test_invoke_drops_client_on_unknown_error(monkeypatch):
    """An unknown transport error returns code -3 and discards the cached client."""
    adapter = MagicMock()
    adapter.is_closed = False
    adapter.request = AsyncMock(side_effect=RuntimeError("link broken"))
    adapter.close = AsyncMock()
    monkeypatch.setattr(zenoh_rpc, "connect", AsyncMock(return_value=adapter))

    result = await zenoh_rpc.invoke("t", "dev", "fn", {})

    assert result["error"]["code"] == -3
    adapter.close.assert_awaited_once()  # stale client dropped
    assert zenoh_rpc._invoke_client is None


@pytest.mark.asyncio
async def test_invoke_no_responders_maps_to_minus_one(monkeypatch):
    adapter = MagicMock()
    adapter.is_closed = False
    adapter.request = AsyncMock(side_effect=Exception("Request timed out (no responders)"))
    monkeypatch.setattr(zenoh_rpc, "connect", AsyncMock(return_value=adapter))

    result = await zenoh_rpc.invoke("t", "dev", "fn", {})
    assert result["error"]["code"] == -1


@pytest.mark.asyncio
async def test_run_verification_detects_toplevel_acl(monkeypatch, tmp_path):
    """ACL enabled at top-level 'access_control' is reported as a pass.

    Before the fix the check read 'plugins.access_control' and always failed.
    """
    from device_connect_server.portal.services import zenoh_backend, zenoh_acl, zenoh_pki
    from device_connect_server.portal import config as portal_config

    # CA + privileged creds present.
    monkeypatch.setattr(zenoh_pki, "ca_exists", lambda: True)
    monkeypatch.setattr(zenoh_pki, "get_ca_fingerprint", AsyncMock(return_value="ab" * 20))
    monkeypatch.setattr(portal_config, "CREDS_DIR", tmp_path)
    for name in ("registry", "facilitator"):
        (tmp_path / f"{name}.creds.json").write_text("{}")

    # Config with ACL at the correct top-level location.
    monkeypatch.setattr(zenoh_acl, "load_config", lambda: {
        "access_control": {"enabled": True, "default_permission": "deny", "rules": []},
    })
    monkeypatch.setattr(zenoh_acl, "list_tenant_rules", lambda: {})

    backend = zenoh_backend.ZenohBackend()
    results = await backend.run_verification()

    acl_result = next(r for r in results if r["name"] == "Zenoh ACL Plugin")
    assert acl_result["status"] == "pass"
    assert "default_permission=deny" in acl_result["detail"]

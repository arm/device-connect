# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for ``portal.views.devices.revoke_credential``.

Covers two failure modes flagged in PR #38 review:

1. A post-revoke ``reload_broker()`` failure must NOT strand the
   credential file. Once ``remove_device()`` succeeds the account is
   gone; ``reload_broker()`` is independently retryable, so its failure
   is a non-blocking warning, not a 502 that leaves the file behind
   (a retry would re-call ``remove_device()`` against an already-deleted
   account and fail forever).
2. A credential file with no/empty ``tenant`` must be refused (422)
   rather than calling ``remove_device("", name)`` against the wrong
   namespace — which for an admin user would otherwise bypass the
   tenant-match check and leave the real account live.
"""

import pytest
from aiohttp import web

from device_connect_server.portal.views import devices


class FakeRequest:
    """Minimal stand-in: ``revoke_credential`` only touches
    ``request["user"]`` and ``request.match_info["name"]``."""

    def __init__(self, user, name):
        self._user = user
        self.match_info = {"name": name}

    def __getitem__(self, key):
        if key == "user":
            return self._user
        raise KeyError(key)


class FakeBackend:
    def __init__(self, *, remove_exc=None, reload_exc=None):
        self._remove_exc = remove_exc
        self._reload_exc = reload_exc
        self.remove_calls = []
        self.reload_calls = 0

    async def remove_device(self, tenant, device_name):
        self.remove_calls.append((tenant, device_name))
        if self._remove_exc is not None:
            raise self._remove_exc

    async def reload_broker(self):
        self.reload_calls += 1
        if self._reload_exc is not None:
            raise self._reload_exc

    def backend_name(self):
        return "fake"


@pytest.fixture
def patched(monkeypatch):
    """Patch credential I/O + backend; track delete_credential calls."""
    state = {"deleted": [], "cred_data": {"tenant": "alpha"}, "backend": None}

    monkeypatch.setattr(
        devices.credentials, "get_credential_data",
        lambda filename: state["cred_data"],
    )

    def _delete(filename):
        state["deleted"].append(filename)
        return True

    monkeypatch.setattr(devices.credentials, "delete_credential", _delete)
    monkeypatch.setattr(devices, "get_backend", lambda: state["backend"])
    return state


async def test_reload_failure_after_remove_still_deletes_file(patched):
    """remove() succeeds, reload_broker() fails -> file is still deleted,
    failure surfaces as a non-blocking warning header, no 502."""
    backend = FakeBackend(reload_exc=TimeoutError("broker timeout"))
    patched["backend"] = backend
    req = FakeRequest({"tenant": "alpha", "role": "user"}, "dev1")

    resp = await devices.revoke_credential(req)

    assert resp.status == 200
    assert patched["deleted"] == ["dev1.creds.json"], (
        "credential file must be deleted even when broker reload fails"
    )
    assert backend.remove_calls == [("alpha", "dev1")]
    assert "broker reload failed" in resp.headers.get("X-Revoke-Warning", "")


async def test_remove_failure_keeps_file_and_returns_502(patched):
    """remove() itself fails -> account still live -> keep the file and
    raise 502 so the operator can retry."""
    backend = FakeBackend(remove_exc=RuntimeError("nsc down"))
    patched["backend"] = backend
    req = FakeRequest({"tenant": "alpha", "role": "user"}, "dev1")

    with pytest.raises(web.HTTPBadGateway):
        await devices.revoke_credential(req)

    assert patched["deleted"] == [], "file must be kept on hard backend failure"
    assert backend.reload_calls == 0


async def test_missing_tenant_rejected_even_for_admin(patched):
    """A credential file with no tenant is refused (422) before any
    backend call — an admin must not slip past into remove("", name)."""
    patched["cred_data"] = {"device_name": "dev1"}  # no "tenant" key
    backend = FakeBackend()
    patched["backend"] = backend
    req = FakeRequest({"tenant": "alpha", "role": "admin"}, "dev1")

    with pytest.raises(web.HTTPUnprocessableEntity):
        await devices.revoke_credential(req)

    assert backend.remove_calls == []
    assert patched["deleted"] == []

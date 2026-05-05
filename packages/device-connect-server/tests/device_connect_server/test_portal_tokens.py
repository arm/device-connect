# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for device_connect_server.portal.services.tokens."""

import json
from unittest.mock import patch

import pytest

from device_connect_server.portal.services import tokens as tokens_svc


class _FakeEtcd:
    """In-memory etcd stub with the get/put/get_prefix surface used by tokens."""

    def __init__(self):
        self.kv: dict[str, str] = {}

    def get(self, key):
        v = self.kv.get(key)
        return [v] if v is not None else []

    def put(self, key, value):
        self.kv[key] = value

    def get_prefix(self, prefix):
        out = []
        for k, v in self.kv.items():
            if k.startswith(prefix):
                out.append((v, {"key": k}))
        return out


@pytest.fixture
def fake_etcd():
    fake = _FakeEtcd()
    with patch.object(tokens_svc, "_etcd_client", return_value=fake):
        yield fake


# ── creation ────────────────────────────────────────────────────────


class TestCreateToken:
    def test_returns_token_secret_only_on_create(self, fake_etcd):
        out = tokens_svc.create_token(
            username="alice", tenant="acme", role="user",
            scopes=["devices:read"], label="ci",
        )
        assert out["token"].startswith("dcp_")
        assert out["username"] == "alice"
        assert out["tenant"] == "acme"
        assert out["scopes"] == ["devices:read"]
        # Listing must never expose the secret or its hash
        listed = tokens_svc.list_tokens()
        assert listed
        for r in listed:
            assert "secret_hash" not in r
            assert "token" not in r

    def test_unknown_scope_rejected(self, fake_etcd):
        with pytest.raises(ValueError):
            tokens_svc.create_token(
                username="alice", tenant="acme", role="user",
                scopes=["devices:read", "made:up"],
            )

    def test_scopes_deduped_and_sorted(self, fake_etcd):
        out = tokens_svc.create_token(
            username="a", tenant="t", role="user",
            scopes=["devices:invoke", "devices:read", "devices:invoke"],
        )
        assert out["scopes"] == ["devices:invoke", "devices:read"]


# ── verification ────────────────────────────────────────────────────


class TestVerifyToken:
    def test_round_trip(self, fake_etcd):
        created = tokens_svc.create_token(
            username="bob", tenant="acme", role="user", scopes=["devices:read"],
        )
        verified = tokens_svc.verify_token(created["token"])
        assert verified is not None
        assert verified["username"] == "bob"
        assert verified["scopes"] == ["devices:read"]
        assert "secret_hash" not in verified

    def test_malformed_token_rejected(self, fake_etcd):
        assert tokens_svc.verify_token("") is None
        assert tokens_svc.verify_token("not-a-token") is None
        assert tokens_svc.verify_token("dcp_only") is None
        assert tokens_svc.verify_token("dcp_id_") is None

    def test_unknown_id_rejected(self, fake_etcd):
        assert tokens_svc.verify_token("dcp_aaaaaaaaaaaaaaaa_secret") is None

    def test_wrong_secret_rejected(self, fake_etcd):
        created = tokens_svc.create_token(
            username="bob", tenant="acme", role="user", scopes=["devices:read"],
        )
        token = created["token"]
        # Mutate the secret tail
        head, tail = token.rsplit("_", 1)
        bad = f"{head}_{tail[:-1]}X"
        assert tokens_svc.verify_token(bad) is None

    def test_revoked_rejected(self, fake_etcd):
        created = tokens_svc.create_token(
            username="bob", tenant="acme", role="user", scopes=["devices:read"],
        )
        assert tokens_svc.revoke_token(created["token_id"]) is True
        assert tokens_svc.verify_token(created["token"]) is None

    def test_expired_rejected(self, fake_etcd):
        from datetime import datetime, timedelta, timezone
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        created = tokens_svc.create_token(
            username="bob", tenant="acme", role="user",
            scopes=["devices:read"], expires_at=past,
        )
        assert tokens_svc.verify_token(created["token"]) is None


# ── scope checks ────────────────────────────────────────────────────


class TestScopes:
    def test_has_scope_direct(self):
        assert tokens_svc.has_scope({"scopes": ["devices:read"]}, "devices:read")
        assert not tokens_svc.has_scope({"scopes": ["devices:read"]}, "devices:invoke")

    def test_admin_wildcard_grants_admin(self):
        assert tokens_svc.has_scope({"scopes": ["admin:*"]}, "admin:tenants")

    def test_admin_wildcard_does_not_grant_devices_scope(self):
        # admin:* covers admin:* family only — explicit devices:* is still required
        assert not tokens_svc.has_scope({"scopes": ["admin:*"]}, "devices:invoke")

    def test_no_scopes(self):
        assert not tokens_svc.has_scope({}, "devices:read")

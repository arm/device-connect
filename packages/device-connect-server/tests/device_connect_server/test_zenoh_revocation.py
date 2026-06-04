# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for Zenoh device revocation: ACL CN removal + key-file deletion.

Revocation must actually drop the device's cert CN from the router ACL
(otherwise the cert stays authorized) and remove its key material from
disk. These cover the pure config/file mechanics; the live "revoked
device is denied after reload" behavior is exercised end-to-end.
"""

import json

import pytest

from device_connect_server.portal.services import zenoh_acl, zenoh_pki


@pytest.fixture
def infra(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "device_connect_server.portal.config.SECURITY_INFRA_DIR", tmp_path)
    return tmp_path


def _acl(infra):
    return json.loads((infra / "zenoh-config.json5").read_text())["access_control"]


def test_remove_one_cn_keeps_the_others(infra):
    zenoh_acl.add_tenant_rule("acme", ["acme-a", "acme-b"])
    zenoh_acl.remove_devices_from_tenant("acme", ["acme-a"])

    acl = _acl(infra)
    subj = next(s for s in acl["subjects"] if s["id"] == "tenant-acme")
    assert subj["cert_common_names"] == ["acme-b"]
    # Rule + policy still present while the tenant has a device.
    assert any(r["id"] == "tenant-acme" for r in acl["rules"])
    assert any("tenant-acme" in p.get("subjects", []) for p in acl["policies"])


def test_remove_last_cn_prunes_subject_rule_policy(infra):
    zenoh_acl.add_tenant_rule("acme", ["acme-a"])
    zenoh_acl.remove_devices_from_tenant("acme", ["acme-a"])

    acl = _acl(infra)
    assert all(s["id"] != "tenant-acme" for s in acl["subjects"])
    assert all(r["id"] != "tenant-acme" for r in acl["rules"])
    assert all("tenant-acme" not in p.get("subjects", []) for p in acl["policies"])


def test_remove_is_idempotent_on_unknown(infra):
    zenoh_acl.add_tenant_rule("acme", ["acme-a"])
    # Unknown tenant and unknown CN are both no-ops, not errors.
    zenoh_acl.remove_devices_from_tenant("ghost", ["x"])
    zenoh_acl.remove_devices_from_tenant("acme", ["not-a-device"])
    subj = next(s for s in _acl(infra)["subjects"] if s["id"] == "tenant-acme")
    assert subj["cert_common_names"] == ["acme-a"]


def test_delete_client_cert_removes_files_idempotently(infra):
    (infra / "dev1-cert.pem").write_text("CERT")
    (infra / "dev1-key.pem").write_text("KEY")

    zenoh_pki.delete_client_cert("dev1")
    assert not (infra / "dev1-cert.pem").exists()
    assert not (infra / "dev1-key.pem").exists()

    # Second call on already-gone files must not raise.
    zenoh_pki.delete_client_cert("dev1")

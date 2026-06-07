# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Per-tenant-CN ACL model + revocation semantics.

Under per-tenant-CN every device of a tenant presents a cert with
``CN=tenant``, so the ACL has ONE static subject per tenant and device
add/remove never touch it (that is what makes provisioning reload-free).
The consequence is that per-device revocation is *soft* -- the only
cert-level cutoff is hard-revoking the whole tenant. See
docs/zenoh-per-tenant-cn.md.
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


def test_tenant_rule_uses_the_tenant_as_cn(infra):
    zenoh_acl.add_tenant_rule("acme")
    acl = _acl(infra)

    subj = next(s for s in acl["subjects"] if s["id"] == "tenant-acme")
    assert subj["cert_common_names"] == ["acme"]          # tenant CN, not device CNs
    rule = next(r for r in acl["rules"] if r["id"] == "tenant-acme")
    assert rule["key_exprs"] == ["device-connect/acme/**"]
    assert any("tenant-acme" in p.get("subjects", []) for p in acl["policies"])


def test_add_tenant_rule_is_idempotent(infra):
    zenoh_acl.add_tenant_rule("acme")
    first = (infra / "zenoh-config.json5").read_text()
    zenoh_acl.add_tenant_rule("acme")          # second call must not change anything
    assert (infra / "zenoh-config.json5").read_text() == first
    acl = _acl(infra)
    assert sum(1 for s in acl["subjects"] if s["id"] == "tenant-acme") == 1


def test_adding_a_device_does_not_touch_the_acl(infra):
    zenoh_acl.add_tenant_rule("acme")
    before = (infra / "zenoh-config.json5").read_text()
    # The device-add path: ensures the rule exists, never lists the device CN.
    zenoh_acl.add_devices_to_tenant("acme", ["acme-cam-001"])
    after = (infra / "zenoh-config.json5").read_text()
    assert before == after
    assert _acl(infra)["subjects"][-1]["cert_common_names"] == ["acme"]


def test_removing_a_device_is_a_noop(infra):
    zenoh_acl.add_tenant_rule("acme")
    before = (infra / "zenoh-config.json5").read_text()
    # Soft revocation: per-device ACL removal is impossible (shared CN).
    zenoh_acl.remove_devices_from_tenant("acme", ["acme-cam-001"])
    after = (infra / "zenoh-config.json5").read_text()
    assert before == after
    # The tenant rule (and thus every device) is still authorized.
    assert any(s["id"] == "tenant-acme" for s in _acl(infra)["subjects"])


def test_remove_tenant_rule_hard_revokes_the_whole_tenant(infra):
    zenoh_acl.add_tenant_rule("acme")
    zenoh_acl.add_tenant_rule("beta")
    zenoh_acl.remove_tenant_rule("acme")

    acl = _acl(infra)
    assert all(s["id"] != "tenant-acme" for s in acl["subjects"])
    assert all(r["id"] != "tenant-acme" for r in acl["rules"])
    assert all("tenant-acme" not in p.get("subjects", []) for p in acl["policies"])
    # Other tenants are untouched.
    assert any(s["id"] == "tenant-beta" for s in acl["subjects"])


def test_remove_tenant_rule_is_idempotent(infra):
    zenoh_acl.add_tenant_rule("acme")
    zenoh_acl.remove_tenant_rule("acme")
    before = (infra / "zenoh-config.json5").read_text()
    zenoh_acl.remove_tenant_rule("acme")   # already gone -> no change
    assert (infra / "zenoh-config.json5").read_text() == before


def test_delete_client_cert_removes_files_idempotently(infra):
    (infra / "dev1-cert.pem").write_text("CERT")
    (infra / "dev1-key.pem").write_text("KEY")
    zenoh_pki.delete_client_cert("dev1")
    assert not (infra / "dev1-cert.pem").exists()
    assert not (infra / "dev1-key.pem").exists()
    zenoh_pki.delete_client_cert("dev1")   # second call must not raise

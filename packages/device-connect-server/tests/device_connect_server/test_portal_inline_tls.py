# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``portal.services.credentials.inline_tls_material``.

mTLS (Zenoh) device credentials reference their CA/cert/key by absolute host
path. Those paths only exist on the portal machine, so a downloaded
``*.creds.json`` is useless on a remote edge host. ``inline_tls_material``
makes the served credential self-contained by inlining the PEM text and
dropping the host-absolute paths.
"""

import json

from device_connect_server.portal.services import credentials


def _zenoh_cred(ca, cert, key):
    return {
        "device_id": "acme-cam-001",
        "auth_type": "mtls",
        "tenant": "acme",
        "zenoh": {
            "urls": ["zenoh+tls://host:7447"],
            "tls": {"ca_file": str(ca), "cert_file": str(cert), "key_file": str(key)},
        },
    }


def test_inlines_pem_and_drops_paths(tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("CA-PEM")
    cert = tmp_path / "c.pem"
    cert.write_text("CERT-PEM")
    key = tmp_path / "k.pem"
    key.write_text("KEY-PEM")

    out = credentials.inline_tls_material(_zenoh_cred(ca, cert, key))
    tls = out["zenoh"]["tls"]

    assert tls["ca_pem"] == "CA-PEM"
    assert tls["cert_pem"] == "CERT-PEM"
    assert tls["key_pem"] == "KEY-PEM"
    # Host-absolute paths are removed from the served copy.
    assert "ca_file" not in tls and "cert_file" not in tls and "key_file" not in tls
    # Non-TLS fields are preserved.
    assert out["device_id"] == "acme-cam-001"
    assert out["zenoh"]["urls"] == ["zenoh+tls://host:7447"]


def test_does_not_mutate_input(tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("CA-PEM")
    cert = tmp_path / "c.pem"
    cert.write_text("CERT-PEM")
    key = tmp_path / "k.pem"
    key.write_text("KEY-PEM")
    cred = _zenoh_cred(ca, cert, key)

    credentials.inline_tls_material(cred)

    # Original dict still carries the file paths (we worked on a copy).
    assert cred["zenoh"]["tls"]["ca_file"] == str(ca)
    assert "ca_pem" not in cred["zenoh"]["tls"]


def test_missing_pem_file_is_left_as_is(tmp_path):
    cred = _zenoh_cred(tmp_path / "nope-ca.pem", tmp_path / "nope-c.pem", tmp_path / "nope-k.pem")
    out = credentials.inline_tls_material(cred)
    tls = out["zenoh"]["tls"]
    # Unreadable files: keep the original path, don't raise, don't invent PEMs.
    assert tls["ca_file"] == str(tmp_path / "nope-ca.pem")
    assert "ca_pem" not in tls


def test_nats_jwt_cred_unchanged():
    cred = {
        "device_id": "acme-cam-001",
        "auth_type": "jwt",
        "tenant": "acme",
        "nats": {"urls": ["nats://host:4222"], "jwt": "JWT", "nkey_seed": "SEED"},
    }
    out = credentials.inline_tls_material(cred)
    assert out == cred


def test_result_is_json_serializable(tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("CA-PEM")
    cert = tmp_path / "c.pem"
    cert.write_text("CERT-PEM")
    key = tmp_path / "k.pem"
    key.write_text("KEY-PEM")
    out = credentials.inline_tls_material(_zenoh_cred(ca, cert, key))
    # Round-trips cleanly (this is what the download handlers serialize).
    assert json.loads(json.dumps(out)) == out

# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Zenoh ACL config management — generates and updates Zenoh router config.

The Zenoh 1.0 ACL plugin enforces access control based on TLS certificate CN
and key-expression rules, providing broker-enforced tenant isolation analogous
to NATS JWT subject permissions.
"""

import json
import logging
from pathlib import Path

from .. import config

logger = logging.getLogger(__name__)

# Default Zenoh message types to allow. Zenoh 1.x renamed the ACL message
# verbs: the former "get" is now "query" (with "reply" for the response side).
# Covers pub/sub (put/delete/declare_subscriber) and RPC
# (query/reply/declare_queryable) as used by the edge Zenoh adapter.
_ALL_MESSAGES = [
    "put",
    "delete",
    "query",
    "reply",
    "declare_subscriber",
    "declare_queryable",
]
_ALL_FLOWS = ["ingress", "egress"]


def _config_path() -> Path:
    """Path to the Zenoh router config file."""
    return config.SECURITY_INFRA_DIR / "zenoh-config.json5"


def load_config() -> dict:
    """Load the current Zenoh router config. Returns empty dict if not found."""
    path = _config_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load Zenoh config: %s", e)
        return {}


def save_config(cfg: dict) -> None:
    """Write the Zenoh router config to disk."""
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2))
    logger.info("Wrote Zenoh config: %s", path)


def generate_config(host: str, port: str = "7447") -> dict:
    """Generate the initial Zenoh router config with TLS and ACL.

    Includes privileged rules for registry/facilitator but no tenant rules yet.
    """
    cfg = {
        "mode": "router",
        "listen": {
            "endpoints": [f"tls/0.0.0.0:{port}"],
        },
        "transport": {
            "link": {
                "tls": {
                    "listen_certificate": "/certs/zenoh-cert.pem",
                    "listen_private_key": "/certs/zenoh-key.pem",
                    "root_ca_certificate": "/certs/ca.pem",
                    "enable_mtls": True,
                },
            },
        },
        # Zenoh 1.x exposes access control as a core config field (top-level
        # "access_control"), not a loadable plugin. Nesting it under "plugins"
        # makes zenohd look for libzenoh_plugin_access_control.so (absent from
        # the official image) and silently run with ACL disabled.
        "access_control": {
            "enabled": True,
            "default_permission": "deny",
            "rules": [
                {
                    "id": "privileged",
                    "messages": _ALL_MESSAGES,
                    "flows": _ALL_FLOWS,
                    "key_exprs": ["device-connect/**"],
                    "permission": "allow",
                },
                {
                    "id": "inbox",
                    "messages": _ALL_MESSAGES,
                    "flows": _ALL_FLOWS,
                    "key_exprs": ["@/**"],
                    "permission": "allow",
                },
            ],
            "subjects": [
                {
                    "id": "privileged",
                    "cert_common_names": ["registry", "facilitator"],
                },
                {
                    "id": "all-clients",
                    "cert_common_names": ["*"],
                },
            ],
            "policies": [
                {
                    "rules": ["privileged"],
                    "subjects": ["privileged"],
                },
                {
                    "id": "inbox-policy",
                    "rules": ["inbox"],
                    "subjects": ["all-clients"],
                },
            ],
        },
    }

    save_config(cfg)
    return cfg


def add_tenant_rule(tenant: str) -> dict:
    """Ensure the ACL grants a tenant access to its own namespace.

    Per-tenant-CN model: every device in a tenant presents a client
    certificate whose Common Name (CN) **is the tenant** (the per-device
    identity lives in the cert OU and the application-layer ``device_id``,
    not the CN). So a single static subject per tenant -- matching
    ``cert_common_names: [tenant]`` -- authorizes every current and future
    device of that tenant on ``device-connect/{tenant}/**``.

    The consequence -- and the whole point -- is that adding or removing a
    device never changes the ACL, so device provisioning/revocation needs
    no router restart. Only tenant creation/deletion touches the ACL.

    Idempotent: if the tenant rule already exists the config is returned
    unchanged (no write), so it is safe to call on every provision.
    """
    cfg = load_config()
    acl = cfg.get("access_control", {})
    rule_id = subject_id = f"tenant-{tenant}"

    if any(r.get("id") == rule_id for r in acl.get("rules", [])):
        return cfg  # already present -- no change, no reload needed

    acl.setdefault("rules", []).append({
        "id": rule_id,
        "messages": _ALL_MESSAGES,
        "flows": _ALL_FLOWS,
        "key_exprs": [f"device-connect/{tenant}/**"],
        "permission": "allow",
    })
    acl.setdefault("subjects", []).append({
        "id": subject_id,
        "cert_common_names": [tenant],
    })
    acl.setdefault("policies", []).append({
        "rules": [rule_id],
        "subjects": [subject_id],
    })

    cfg["access_control"] = acl
    save_config(cfg)
    logger.info("Added tenant ACL rule: %s (CN=%s)", tenant, tenant)
    return cfg


# Back-compat alias for callers that "add a device": under per-tenant-CN a
# device carries no individual ACL entry, so this just ensures the tenant
# rule exists (a no-op once the tenant has been created).
def add_devices_to_tenant(tenant: str, device_cns: list[str] | None = None) -> dict:
    """Deprecated under per-tenant-CN: ensure the tenant rule exists.

    All of a tenant's devices share ``CN=tenant``, so device CNs are no
    longer listed individually in the ACL. ``device_cns`` is ignored.
    """
    return add_tenant_rule(tenant)


def remove_devices_from_tenant(tenant: str, device_cns: list[str] | None = None) -> dict:
    """No-op under per-tenant-CN: a single device cannot be removed from the
    ACL because all of a tenant's devices share ``CN=tenant``.

    Per-device revocation is therefore *soft* -- delete the credential/cert
    so it cannot be re-issued, but the certificate stays cryptographically
    valid until it expires. Use :func:`remove_tenant_rule` to hard-revoke an
    entire tenant, and short-lived device certificates to bound the
    soft-revocation window. See ``docs/zenoh-per-tenant-cn.md``.
    """
    logger.info(
        "remove_devices_from_tenant is a no-op under per-tenant-CN "
        "(soft revocation); tenant=%s", tenant)
    return load_config()


def remove_tenant_rule(tenant: str) -> dict:
    """Hard-revoke an entire tenant: drop its ACL subject/rule/policy.

    After the router reloads, no certificate with ``CN=tenant`` is
    authorized for ``device-connect/{tenant}/**`` -- the only
    certificate-level cutoff available under the shared-CN model. Idempotent.
    """
    cfg = load_config()
    acl = cfg.get("access_control", {})
    rule_id = subject_id = f"tenant-{tenant}"

    subjects = [s for s in acl.get("subjects", []) if s.get("id") != subject_id]
    rules = [r for r in acl.get("rules", []) if r.get("id") != rule_id]
    policies = [
        p for p in acl.get("policies", [])
        if subject_id not in p.get("subjects", []) and rule_id not in p.get("rules", [])
    ]
    unchanged = (
        len(subjects) == len(acl.get("subjects", []))
        and len(rules) == len(acl.get("rules", []))
        and len(policies) == len(acl.get("policies", []))
    )
    if unchanged:
        return cfg  # nothing matched -- no change, no reload needed

    acl["subjects"], acl["rules"], acl["policies"] = subjects, rules, policies
    cfg["access_control"] = acl
    save_config(cfg)
    logger.info("Removed tenant ACL rule (hard revocation): %s", tenant)
    return cfg


def get_tenant_cns(tenant: str) -> list[str]:
    """Get the list of device CNs for a tenant."""
    cfg = load_config()
    acl = cfg.get("access_control", {})
    subject_id = f"tenant-{tenant}"
    for subject in acl.get("subjects", []):
        if subject["id"] == subject_id:
            return subject.get("cert_common_names", [])
    return []


def list_tenant_rules() -> dict[str, list[str]]:
    """List all tenant rules and their device CNs.

    Returns dict: tenant_name -> [device_cn, ...].
    """
    cfg = load_config()
    acl = cfg.get("access_control", {})
    tenants = {}
    for subject in acl.get("subjects", []):
        sid = subject["id"]
        if sid.startswith("tenant-"):
            tenant = sid[len("tenant-"):]
            tenants[tenant] = subject.get("cert_common_names", [])
    return tenants

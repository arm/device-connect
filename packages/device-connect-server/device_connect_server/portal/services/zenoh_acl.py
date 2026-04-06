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

# Default Zenoh message types to allow
_ALL_MESSAGES = ["put", "get", "declare_subscriber", "declare_queryable"]
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
                    "server_certificate": "/certs/zenoh-cert.pem",
                    "server_private_key": "/certs/zenoh-key.pem",
                    "root_ca_certificate": "/certs/ca.pem",
                    "client_auth": True,
                },
            },
        },
        "plugins": {
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
        },
    }

    save_config(cfg)
    return cfg


def add_tenant_rule(tenant: str, device_cns: list[str]) -> dict:
    """Add ACL rules for a new tenant.

    Creates:
      - A rule allowing access to device-connect/{tenant}/**
      - A subject group with the device CNs
      - A policy linking them

    Returns the updated config.
    """
    cfg = load_config()
    acl = cfg.get("plugins", {}).get("access_control", {})

    rule_id = f"tenant-{tenant}"
    subject_id = f"tenant-{tenant}"

    # Check if tenant rule already exists
    existing_rules = {r["id"] for r in acl.get("rules", [])}
    if rule_id in existing_rules:
        # Just add the new CNs to the existing subject group
        return add_devices_to_tenant(tenant, device_cns)

    # Add rule
    acl.setdefault("rules", []).append({
        "id": rule_id,
        "messages": _ALL_MESSAGES,
        "flows": _ALL_FLOWS,
        "key_exprs": [f"device-connect/{tenant}/**"],
        "permission": "allow",
    })

    # Add subject group
    acl.setdefault("subjects", []).append({
        "id": subject_id,
        "cert_common_names": list(device_cns),
    })

    # Add policy
    acl.setdefault("policies", []).append({
        "rules": [rule_id],
        "subjects": [subject_id],
    })

    cfg.setdefault("plugins", {})["access_control"] = acl
    save_config(cfg)
    logger.info("Added tenant ACL rule: %s (%d devices)", tenant, len(device_cns))
    return cfg


def add_devices_to_tenant(tenant: str, device_cns: list[str]) -> dict:
    """Add device CNs to an existing tenant's ACL subject group.

    Returns the updated config.
    """
    cfg = load_config()
    acl = cfg.get("plugins", {}).get("access_control", {})

    subject_id = f"tenant-{tenant}"
    for subject in acl.get("subjects", []):
        if subject["id"] == subject_id:
            existing = set(subject.get("cert_common_names", []))
            existing.update(device_cns)
            subject["cert_common_names"] = sorted(existing)
            break
    else:
        # Subject group doesn't exist — create the full tenant rule
        return add_tenant_rule(tenant, device_cns)

    cfg["plugins"]["access_control"] = acl
    save_config(cfg)
    logger.info("Added %d device(s) to tenant %s ACL", len(device_cns), tenant)
    return cfg


def get_tenant_cns(tenant: str) -> list[str]:
    """Get the list of device CNs for a tenant."""
    cfg = load_config()
    acl = cfg.get("plugins", {}).get("access_control", {})
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
    acl = cfg.get("plugins", {}).get("access_control", {})
    tenants = {}
    for subject in acl.get("subjects", []):
        sid = subject["id"]
        if sid.startswith("tenant-"):
            tenant = sid[len("tenant-"):]
            tenants[tenant] = subject.get("cert_common_names", [])
    return tenants

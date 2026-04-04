"""Portal configuration — paths, env vars, defaults."""

import os
from pathlib import Path

# Portal server
PORTAL_PORT = int(os.environ.get("PORTAL_PORT", "8080"))
PORTAL_HOST = os.environ.get("PORTAL_HOST", "0.0.0.0")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "device-connect-portal-secret-change-me")

# NATS
NATS_HOST = os.environ.get("NATS_HOST", "localhost")
NATS_PORT = os.environ.get("NATS_PORT", "4222")
NATS_CONTAINER = os.environ.get("NATS_CONTAINER", "dc-nats")

# etcd
ETCD_HOST = os.environ.get("ETCD_HOST", "localhost")
ETCD_PORT = int(os.environ.get("ETCD_PORT", "2379"))

# Admin credentials
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "qwe123")

# Paths
SECURITY_INFRA_DIR = Path(os.environ.get(
    "SECURITY_INFRA_DIR",
    Path(__file__).resolve().parent.parent.parent / "security_infra",
))
NSC_HOME = SECURITY_INFRA_DIR / ".nsc"
CREDS_DIR = Path(os.environ.get(
    "CREDS_DIR",
    Path.home() / ".device-connect" / "credentials",
))
BUNDLES_DIR = SECURITY_INFRA_DIR / "tenant-bundles"

# NSC account name
NSC_ACCOUNT = os.environ.get("DC_NSC_ACCOUNT", "DEVICE_CONNECT")
NSC_OPERATOR = os.environ.get("DC_NSC_OPERATOR", "device-connect-operator")

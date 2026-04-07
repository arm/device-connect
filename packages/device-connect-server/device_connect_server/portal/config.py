"""Portal configuration — paths, env vars, defaults."""

import os
import secrets
from pathlib import Path

# Portal server
PORTAL_PORT = int(os.environ.get("PORTAL_PORT", "8080"))
PORTAL_HOST = os.environ.get("PORTAL_HOST", "0.0.0.0")
SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_urlsafe(32)

# Messaging backend (optional override; otherwise auto-detected from etcd)
MESSAGING_BACKEND = os.environ.get("MESSAGING_BACKEND", "")

# NATS
NATS_HOST = os.environ.get("NATS_HOST", "localhost")
NATS_PORT = os.environ.get("NATS_PORT", "4222")
NATS_CONTAINER = os.environ.get("NATS_CONTAINER", "dc-nats")

# Zenoh
ZENOH_HOST = os.environ.get("ZENOH_HOST", "localhost")
ZENOH_PORT = os.environ.get("ZENOH_PORT", "7447")
ZENOH_CONTAINER = os.environ.get("ZENOH_CONTAINER", "dc-zenoh")

# MQTT (Mosquitto)
MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = os.environ.get("MQTT_PORT", "1883")
MQTT_CONTAINER = os.environ.get("MQTT_CONTAINER", "dc-mosquitto")

# etcd
ETCD_HOST = os.environ.get("ETCD_HOST", "localhost")
ETCD_PORT = int(os.environ.get("ETCD_PORT", "2379"))

# Admin credentials
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS") or secrets.token_urlsafe(16)
ADMIN_PASS_GENERATED = "ADMIN_PASS" not in os.environ

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

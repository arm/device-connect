"""Configuration for MCP Bridge and DeviceConnectMCP devices.

Loads configuration from environment variables and credentials files.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class BridgeConfig:
    """Configuration for MCP Bridge Server and DeviceConnectMCP devices.

    Can be loaded from:
    - Environment variables (NATS_URL, NATS_CREDENTIALS_FILE, etc.)
    - Credentials file (.creds.json format)
    - Direct parameters

    Example:
        # From environment
        config = BridgeConfig.from_environment()

        # From credentials file
        config = BridgeConfig.from_credentials_file("/path/to/creds.json")

        # Direct
        config = BridgeConfig(
            messaging_urls=["tcp/localhost:7447"],
            tenant="default",
        )
    """

    # Messaging configuration
    messaging_urls: List[str] = field(default_factory=lambda: ["tcp/localhost:7447"])
    messaging_auth: Optional[Dict[str, Any]] = None
    messaging_tls: Optional[Dict[str, Any]] = None

    # Device Connect configuration
    tenant: str = "default"

    # Discovery mode: "auto" (detect from backend/URLs), "d2d", or "infra"
    discovery_mode: str = "auto"

    # MCP Bridge configuration
    refresh_interval: float = 30.0  # Seconds between device refreshes
    request_timeout: float = 30.0   # Tool call timeout in seconds

    @classmethod
    def from_environment(cls) -> "BridgeConfig":
        """Load configuration from environment variables.

        Environment variables:
            MESSAGING_URLS: Broker URLs (comma-separated)
            ZENOH_CONNECT: Zenoh endpoints (comma-separated)
            NATS_URL: NATS server URL (when using NATS backend)
            NATS_CREDENTIALS_FILE: Path to .creds.json file
            NATS_TLS_CA_FILE: Path to CA certificate
            TENANT: Device Connect tenant (default: "default")
            MCP_REFRESH_INTERVAL: Tool refresh interval (default: 30)
            MCP_REQUEST_TIMEOUT: Tool call timeout (default: 30)

        Returns:
            BridgeConfig instance
        """
        # Check for credentials file first (simplest config)
        creds_file = os.getenv("NATS_CREDENTIALS_FILE")
        if creds_file and creds_file.endswith(".creds.json"):
            return cls.from_credentials_file(creds_file)

        # Build from individual env vars (check generic, then Zenoh, then NATS)
        urls_str = (
            os.getenv("MESSAGING_URLS")
            or os.getenv("ZENOH_CONNECT")
            or os.getenv("NATS_URL")
            or "tcp/localhost:7447"
        )
        urls = [u.strip() for u in urls_str.split(",")]

        # TLS configuration
        tls_config = None
        ca_file = os.getenv("NATS_TLS_CA_FILE")
        if ca_file:
            tls_config = {"ca_file": ca_file}

        # Auth from legacy .creds file
        auth = None
        if creds_file and creds_file.endswith(".creds"):
            auth = {"credentials_file": creds_file}

        return cls(
            messaging_urls=urls,
            messaging_auth=auth,
            messaging_tls=tls_config,
            tenant=os.getenv("TENANT", "default"),
            discovery_mode=os.getenv("DEVICE_CONNECT_DISCOVERY_MODE", "auto").lower(),
            refresh_interval=float(os.getenv("MCP_REFRESH_INTERVAL", "30")),
            request_timeout=float(os.getenv("MCP_REQUEST_TIMEOUT", "30")),
        )

    @classmethod
    def from_credentials_file(cls, path: str) -> "BridgeConfig":
        """Load configuration from a .creds.json file.

        The .creds.json format bundles all connection info:
        {
            "device_id": "...",
            "tenant": "default",
            "nats": {
                "urls": ["nats://server:4222"],
                "jwt": "eyJ...",
                "nkey_seed": "SUACX...",
                "tls_ca_file": "/path/to/ca.pem"
            }
        }

        Args:
            path: Path to the credentials file

        Returns:
            BridgeConfig instance
        """
        with open(path, "r") as f:
            data = json.load(f)

        nats_config = data.get("nats", {})

        # Extract URLs
        urls = nats_config.get("urls", ["tcp/localhost:7447"])
        if isinstance(urls, str):
            urls = [urls]

        # Build auth dict
        auth = {}
        if "jwt" in nats_config:
            auth["jwt"] = nats_config["jwt"]
        if "nkey_seed" in nats_config:
            auth["nkey_seed"] = nats_config["nkey_seed"]

        # Build TLS config
        tls_config = None
        if "tls_ca_file" in nats_config:
            tls_config = {"ca_file": nats_config["tls_ca_file"]}

        return cls(
            messaging_urls=urls,
            messaging_auth=auth if auth else None,
            messaging_tls=tls_config,
            tenant=data.get("tenant", "default"),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging/debugging."""
        return {
            "messaging_urls": self.messaging_urls,
            "messaging_auth": "***" if self.messaging_auth else None,
            "messaging_tls": self.messaging_tls,
            "tenant": self.tenant,
            "refresh_interval": self.refresh_interval,
            "request_timeout": self.request_timeout,
        }

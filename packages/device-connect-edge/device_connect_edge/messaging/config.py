"""
Configuration utilities for messaging layer.
"""

import os
import json
from typing import Dict, List, Any, Optional
from pathlib import Path


class MessagingConfig:
    """
    Configuration manager for messaging backend.

    Loads configuration from environment variables, config files, or direct parameters.
    """

    def __init__(
        self,
        backend: Optional[str] = None,
        servers: Optional[List[str]] = None,
        credentials: Optional[Dict[str, Any]] = None,
        tls_config: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize messaging configuration.

        Args:
            backend: Messaging backend ("zenoh", "nats", or "mqtt")
            servers: List of broker URLs
            credentials: Authentication credentials
            tls_config: TLS configuration
        """
        self.backend = backend or self._get_backend_from_env()
        self.servers = servers or self._get_servers_from_env()
        self.credentials = credentials or self._get_credentials_from_env()
        self.tls_config = tls_config or self._get_tls_config_from_env()

    @staticmethod
    def _get_backend_from_env() -> str:
        """Get messaging backend from environment.

        Auto-detects zenoh when ZENOH_CONNECT is set.
        """
        explicit = os.getenv("MESSAGING_BACKEND")
        if explicit:
            return explicit.lower()

        # Auto-detect zenoh if ZENOH_CONNECT is set
        if os.getenv("ZENOH_CONNECT"):
            return "zenoh"

        return "zenoh"

    @staticmethod
    def _get_servers_from_env() -> List[str]:
        """
        Get server URLs from environment.

        Checks:
        1. MESSAGING_URLS (comma-separated)
        2. ZENOH_CONNECT (comma-separated Zenoh endpoints)
        3. NATS_URLS (comma-separated, legacy)
        4. NATS_URL (single server, legacy)
        5. Default: ["tcp/localhost:7447"]
        """
        # Check new env var
        urls = os.getenv("MESSAGING_URLS")
        if urls:
            return [url.strip() for url in urls.split(",")]

        # Check Zenoh-specific env var
        zenoh_connect = os.getenv("ZENOH_CONNECT")
        if zenoh_connect:
            return [ep.strip() for ep in zenoh_connect.split(",")]

        # Check legacy NATS env vars
        urls = os.getenv("NATS_URLS")
        if urls:
            return [url.strip() for url in urls.split(",")]

        url = os.getenv("NATS_URL")
        if url:
            return [url.strip()]

        # Default
        return ["tcp/localhost:7447"]

    @staticmethod
    def _get_credentials_from_env() -> Optional[Dict[str, Any]]:
        """
        Get credentials from environment.

        For NATS JWT:
            - NATS_JWT and NATS_NKEY_SEED
            - Or NATS_CREDENTIALS_FILE (JSON or .creds format)

        For MQTT:
            - MESSAGING_USERNAME and MESSAGING_PASSWORD
        """
        credentials = {}

        # Check for credentials file first
        creds_file = os.getenv("NATS_CREDENTIALS_FILE")
        if creds_file and os.path.exists(creds_file):
            return MessagingConfig._load_credentials_file(creds_file)

        # Check for direct JWT credentials
        jwt = os.getenv("NATS_JWT")
        nkey_seed = os.getenv("NATS_NKEY_SEED")
        if jwt and nkey_seed:
            credentials["jwt"] = jwt
            credentials["nkey_seed"] = nkey_seed
            return credentials

        # Check for MQTT credentials
        username = os.getenv("MESSAGING_USERNAME")
        password = os.getenv("MESSAGING_PASSWORD")
        if username or password:
            credentials["username"] = username
            credentials["password"] = password
            return credentials

        return None if not credentials else credentials

    @staticmethod
    def _load_credentials_file(filepath: str) -> Dict[str, Any]:
        """
        Load credentials from file.

        Supports:
        1. JSON format with nested structure
        2. NATS .creds format (JWT + NKey seed)
        """
        path = Path(filepath)

        # Try JSON format first
        try:
            with open(path, "r") as f:
                data = json.load(f)

            if not isinstance(data, dict):
                return {}

            # Extract NATS credentials if nested
            if "nats" in data:
                nats_config = data["nats"]
                credentials = {}

                if "jwt" in nats_config:
                    credentials["jwt"] = nats_config["jwt"]
                if "nkey_seed" in nats_config:
                    credentials["nkey_seed"] = nats_config["nkey_seed"]

                return credentials

            return data

        except json.JSONDecodeError:
            # Try NATS .creds format
            return MessagingConfig._parse_nats_creds_file(filepath)

    @staticmethod
    def _parse_nats_creds_file(filepath: str) -> Dict[str, Any]:
        """
        Parse NATS .creds file format.

        Format:
            -----BEGIN NATS USER JWT-----
            eyJ0eXAiOiJKV1QiLCJhbGc...
            ------END NATS USER JWT------

            -----BEGIN USER NKEY SEED-----
            SUACX...
            ------END USER NKEY SEED------
        """
        with open(filepath, "r") as f:
            content = f.read()

        credentials = {}

        # Extract JWT
        jwt_start = content.find("-----BEGIN NATS USER JWT-----")
        jwt_end = content.find("------END NATS USER JWT------")
        if jwt_start != -1 and jwt_end != -1:
            jwt = content[jwt_start + len("-----BEGIN NATS USER JWT-----"):jwt_end].strip()
            credentials["jwt"] = jwt

        # Extract NKey seed
        nkey_start = content.find("-----BEGIN USER NKEY SEED-----")
        nkey_end = content.find("------END USER NKEY SEED------")
        if nkey_start != -1 and nkey_end != -1:
            nkey_seed = content[nkey_start + len("-----BEGIN USER NKEY SEED-----"):nkey_end].strip()
            credentials["nkey_seed"] = nkey_seed

        return credentials

    @staticmethod
    def _get_tls_config_from_env() -> Optional[Dict[str, Any]]:
        """
        Get TLS configuration from environment.

        Checks (backend-agnostic first, then backend-specific):
        - MESSAGING_TLS_CA_FILE / NATS_TLS_CA_FILE (CA certificate)
        - MESSAGING_TLS_CERT_FILE / NATS_TLS_CERT_FILE (client certificate for mTLS)
        - MESSAGING_TLS_KEY_FILE / NATS_TLS_KEY_FILE (client key for mTLS)
        """
        tls_config = {}

        ca_file = os.getenv("MESSAGING_TLS_CA_FILE") or os.getenv("NATS_TLS_CA_FILE")
        if ca_file:
            tls_config["ca_file"] = ca_file

        cert_file = os.getenv("MESSAGING_TLS_CERT_FILE") or os.getenv("NATS_TLS_CERT_FILE")
        if cert_file:
            tls_config["cert_file"] = cert_file

        key_file = os.getenv("MESSAGING_TLS_KEY_FILE") or os.getenv("NATS_TLS_KEY_FILE")
        if key_file:
            tls_config["key_file"] = key_file

        return tls_config if tls_config else None

    def to_dict(self) -> Dict[str, Any]:
        """Convert configuration to dictionary."""
        return {
            "backend": self.backend,
            "servers": self.servers,
            "credentials": self.credentials,
            "tls_config": self.tls_config,
        }

    def __repr__(self) -> str:
        # Hide sensitive credentials in repr
        safe_dict = self.to_dict()
        if safe_dict["credentials"]:
            safe_dict["credentials"] = "***REDACTED***"
        return f"MessagingConfig({safe_dict})"

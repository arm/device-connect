# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Credential loading and validation for Device Connect.

This module provides utilities for loading device credentials from
files and environment variables. Supports multiple credential formats:
    - JSON format (.creds.json): Bundled config with JWT, NKey, URLs, TLS
    - NATS .creds format: Standard NATS credential file format

Example:
    from device_connect_server.security import CredentialsLoader

    # Load from file
    creds = CredentialsLoader.load_from_file("/credentials/device.creds.json")

    # Load from environment
    creds = CredentialsLoader.load_from_env()

    # Access credentials
    jwt = creds.get("jwt")
    nkey_seed = creds.get("nkey_seed")
    urls = creds.get("urls", ["nats://localhost:4222"])
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class CredentialsLoader:
    """Utility class for loading and parsing device credentials.

    Supports multiple credential file formats and environment variable
    configuration for flexible deployment scenarios.
    """

    @staticmethod
    def load_from_file(path: str) -> Dict[str, Any]:
        """Load credentials from file.

        Automatically detects file format based on content:
            - JSON format: Complete config bundle
            - NATS .creds format: JWT and NKey seed only

        Args:
            path: Path to credentials file

        Returns:
            Dict containing parsed credentials

        Raises:
            FileNotFoundError: If file doesn't exist
            ValueError: If file format is invalid
        """
        file_path = Path(path)

        if not file_path.exists():
            raise FileNotFoundError(f"Credentials file not found: {path}")

        content = file_path.read_text().strip()

        # Try JSON format first
        if content.startswith("{"):
            return CredentialsLoader._parse_json_format(content, path)

        # Try NATS .creds format
        if "-----BEGIN" in content:
            return CredentialsLoader._parse_nats_creds_format(content)

        raise ValueError(f"Unknown credentials format in {path}")

    @staticmethod
    def _parse_json_format(content: str, path: str) -> Dict[str, Any]:
        """Parse JSON format credentials file.

        Expected structure:
            {
                "device_id": "...",
                "tenant": "default",
                "nats": {
                    "urls": ["tls://nats:4222"],
                    "jwt": "...",
                    "nkey_seed": "...",
                    "tls": {"ca_file": "/certs/ca.pem"}
                }
            }

        Args:
            content: JSON file content
            path: Original file path (for logging)

        Returns:
            Normalized credentials dict
        """
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in {path}: {e}")

        result: Dict[str, Any] = {}

        # Extract top-level fields
        if "device_id" in data:
            result["device_id"] = data["device_id"]

        if "tenant" in data:
            result["tenant"] = data["tenant"]

        # Extract NATS config
        nats_config = data.get("nats", data.get("messaging", {}))

        if "urls" in nats_config:
            result["urls"] = nats_config["urls"]
        elif "url" in nats_config:
            result["urls"] = [nats_config["url"]]

        if "jwt" in nats_config:
            result["jwt"] = nats_config["jwt"]

        if "nkey_seed" in nats_config:
            result["nkey_seed"] = nats_config["nkey_seed"]

        # Extract TLS config
        tls_config = nats_config.get("tls", {})
        if tls_config:
            result["tls"] = {
                "ca_file": tls_config.get("ca_file"),
                "cert_file": tls_config.get("cert_file"),
                "key_file": tls_config.get("key_file"),
            }
            # Remove None values
            result["tls"] = {k: v for k, v in result["tls"].items() if v}

        # MQTT-specific
        mqtt_config = data.get("mqtt", {})
        if "username" in mqtt_config:
            result["username"] = mqtt_config["username"]
        if "password" in mqtt_config:
            result["password"] = mqtt_config["password"]

        return result

    @staticmethod
    def _parse_nats_creds_format(content: str) -> Dict[str, Any]:
        """Parse NATS .creds format file.

        Format:
            -----BEGIN NATS USER JWT-----
            <jwt>
            ------END NATS USER JWT------

            -----BEGIN USER NKEY SEED-----
            <seed>
            ------END USER NKEY SEED------

        Args:
            content: File content

        Returns:
            Dict with jwt and nkey_seed
        """
        result: Dict[str, Any] = {}

        # Extract JWT
        jwt_match = re.search(
            r"-----BEGIN NATS USER JWT-----\s*\n(.+?)\n.*?-----END NATS USER JWT-----",
            content,
            re.DOTALL
        )
        if jwt_match:
            result["jwt"] = jwt_match.group(1).strip()

        # Extract NKey seed
        seed_match = re.search(
            r"-----BEGIN USER NKEY SEED-----\s*\n(.+?)\n.*?-----END USER NKEY SEED-----",
            content,
            re.DOTALL
        )
        if seed_match:
            result["nkey_seed"] = seed_match.group(1).strip()

        return result

    @staticmethod
    def load_from_env() -> Dict[str, Any]:
        """Load credentials from environment variables.

        Checks the following environment variables:
            - NATS_CREDENTIALS_FILE: Path to credentials file
            - NATS_JWT: JWT token
            - NATS_NKEY_SEED: NKey seed
            - NATS_URL / NATS_URLS: Server URL(s)
            - MESSAGING_TLS_CA_FILE / NATS_TLS_CA_FILE: CA certificate path
            - MESSAGING_TLS_CERT_FILE / NATS_TLS_CERT_FILE: Client certificate (mTLS)
            - MESSAGING_TLS_KEY_FILE / NATS_TLS_KEY_FILE: Client key (mTLS)
            - MESSAGING_USERNAME: MQTT username
            - MESSAGING_PASSWORD: MQTT password

        Returns:
            Dict containing credentials from environment
        """
        result: Dict[str, Any] = {}

        # Try loading from credentials file first
        creds_file = os.getenv("NATS_CREDENTIALS_FILE")
        if creds_file and Path(creds_file).exists():
            try:
                file_creds = CredentialsLoader.load_from_file(creds_file)
                result.update(file_creds)
            except Exception as e:
                logger.warning(f"Failed to load credentials file: {e}")

        # Override with explicit environment variables
        if os.getenv("NATS_JWT"):
            result["jwt"] = os.getenv("NATS_JWT")

        if os.getenv("NATS_NKEY_SEED"):
            result["nkey_seed"] = os.getenv("NATS_NKEY_SEED")

        # Server URLs
        urls = os.getenv("NATS_URLS") or os.getenv("NATS_URL")
        if urls:
            result["urls"] = [u.strip() for u in urls.split(",")]

        # TLS configuration (backend-agnostic first, then NATS-specific fallback)
        tls: Dict[str, str] = {}
        ca = os.getenv("MESSAGING_TLS_CA_FILE") or os.getenv("NATS_TLS_CA_FILE")
        if ca:
            tls["ca_file"] = ca
        cert = os.getenv("MESSAGING_TLS_CERT_FILE") or os.getenv("NATS_TLS_CERT_FILE")
        if cert:
            tls["cert_file"] = cert
        key = os.getenv("MESSAGING_TLS_KEY_FILE") or os.getenv("NATS_TLS_KEY_FILE")
        if key:
            tls["key_file"] = key
        if tls:
            result["tls"] = tls

        # MQTT credentials
        if os.getenv("MESSAGING_USERNAME"):
            result["username"] = os.getenv("MESSAGING_USERNAME")
        if os.getenv("MESSAGING_PASSWORD"):
            result["password"] = os.getenv("MESSAGING_PASSWORD")

        # Device ID and tenant
        if os.getenv("DEVICE_ID"):
            result["device_id"] = os.getenv("DEVICE_ID")
        if os.getenv("TENANT"):
            result["tenant"] = os.getenv("TENANT")

        return result

    @staticmethod
    def get_urls(creds: Dict[str, Any], default: List[str] = None) -> List[str]:
        """Extract server URLs from credentials.

        Args:
            creds: Credentials dict
            default: Default URLs if not found

        Returns:
            List of server URLs
        """
        if default is None:
            default = ["nats://localhost:4222"]

        urls = creds.get("urls")
        if urls:
            return urls if isinstance(urls, list) else [urls]

        return default

    @staticmethod
    def has_jwt_auth(creds: Dict[str, Any]) -> bool:
        """Check if credentials contain JWT authentication.

        Args:
            creds: Credentials dict

        Returns:
            True if JWT and NKey seed are present
        """
        return bool(creds.get("jwt") and creds.get("nkey_seed"))

    @staticmethod
    def has_password_auth(creds: Dict[str, Any]) -> bool:
        """Check if credentials contain password authentication.

        Args:
            creds: Credentials dict

        Returns:
            True if username and password are present
        """
        return bool(creds.get("username") and creds.get("password"))

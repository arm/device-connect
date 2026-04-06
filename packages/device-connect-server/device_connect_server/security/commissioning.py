#!/usr/bin/env python3
"""
Device commissioning module for Device Connect.

Provides device-side commissioning mode with PIN validation,
credential provisioning, and secure transition to operational mode.
"""

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Any

try:
    import bcrypt
    _BCRYPT_AVAILABLE = True
except ImportError:
    bcrypt = None  # type: ignore[assignment]
    _BCRYPT_AVAILABLE = False


logger = logging.getLogger(__name__)


@dataclass
class CommissioningPIN:
    """Factory-provisioned PIN for device commissioning."""
    pin: str  # 8-digit PIN
    pin_hash: str  # bcrypt hash
    device_id: str
    device_type: str
    created_at: str
    commissioned: bool = False
    commissioned_at: Optional[str] = None


class CommissioningMode:
    """
    Device commissioning mode - waits for admin to provide PIN and credentials.

    This runs when a device boots for the first time (before operational credentials exist).
    The device validates the PIN locally, then receives operational credentials from the admin.
    """

    def __init__(
        self,
        device_id: str,
        device_type: str,
        factory_pin: str,
        capabilities: list[str],
        nkey_public: Optional[str] = None,
        nkey_seed: Optional[str] = None,
        port: int = 5540
    ):
        """
        Initialize commissioning mode.

        Args:
            device_id: Unique device identifier (vendor-assigned)
            device_type: Type of device (camera, robot, etc.)
            factory_pin: 8-digit PIN (from factory provisioning)
            capabilities: List of device capabilities
            nkey_public: Device NKey public key (for JWT auth)
            nkey_seed: Device NKey seed (for JWT auth)
            port: TCP port for commissioning server
        """
        self.device_id = device_id
        self.device_type = device_type
        self.factory_pin = factory_pin
        self.capabilities = capabilities
        self.nkey_public = nkey_public
        self.nkey_seed = nkey_seed
        self.port = port

        # Security state
        if not _BCRYPT_AVAILABLE:
            raise ImportError(
                "bcrypt is required for device commissioning. "
                "Install with: pip install 'device-connect-server[security]'"
            )
        self.pin_hash = bcrypt.hashpw(factory_pin.encode('utf-8'), bcrypt.gensalt())
        self.commission_attempts = 0
        self.last_attempt_time = 0
        self.commissioned = False

        # Rate limiting config
        self.max_attempts = 3
        self.lockout_duration = 3600  # 1 hour in seconds

        logger.info(f"Device {device_id} entering commissioning mode")

    def _check_rate_limit(self) -> tuple[bool, Optional[int]]:
        """
        Check if device is rate-limited.

        Returns:
            (allowed, seconds_until_unlock)
        """
        if self.commission_attempts >= self.max_attempts:
            elapsed = time.time() - self.last_attempt_time
            remaining = self.lockout_duration - elapsed

            if remaining > 0:
                return False, int(remaining)
            else:
                # Reset after lockout period
                self.commission_attempts = 0
                return True, None

        return True, None

    def validate_pin(self, provided_pin: str) -> tuple[bool, Optional[str]]:
        """
        Validate provided PIN against factory PIN.

        Args:
            provided_pin: PIN provided by admin

        Returns:
            (valid, error_message)
        """
        # Check if already commissioned
        if self.commissioned:
            return False, "Device already commissioned"

        # Check rate limiting
        allowed, lockout_seconds = self._check_rate_limit()
        if not allowed:
            logger.warning(f"Rate limit exceeded, locked for {lockout_seconds}s")
            return False, f"Too many attempts. Locked for {lockout_seconds} seconds"

        # Validate PIN
        self.commission_attempts += 1
        self.last_attempt_time = time.time()

        try:
            if bcrypt.checkpw(provided_pin.encode('utf-8'), self.pin_hash):
                logger.info("PIN validation successful")
                return True, None
            else:
                logger.warning(f"PIN validation failed (attempt {self.commission_attempts}/{self.max_attempts})")
                return False, f"Invalid PIN (attempt {self.commission_attempts}/{self.max_attempts})"
        except Exception as e:
            logger.error(f"PIN validation error: {e}")
            return False, "PIN validation error"

    async def start_commissioning_server(self) -> Dict[str, Any]:
        """
        Start commissioning server and wait for admin to commission device.

        Returns:
            Operational credentials provisioned by admin
        """
        from aiohttp import web

        credentials_received = asyncio.Event()
        received_credentials = {}

        async def handle_commission(request):
            """Handle commissioning request from admin."""
            try:
                data = await request.json()

                # Validate PIN
                provided_pin = data.get('pin', '')
                valid, error = self.validate_pin(provided_pin)

                if not valid:
                    logger.warning(f"Commissioning failed: {error}")
                    return web.json_response({
                        'success': False,
                        'error': error
                    }, status=401)

                # PIN valid, accept credentials
                credentials = data.get('credentials', {})
                if not credentials:
                    return web.json_response({
                        'success': False,
                        'error': 'No credentials provided'
                    }, status=400)

                # Phase 3: Generate attestation token after PIN validation
                # The token is included in the credentials bundle so it can
                # be submitted during subsequent device registrations.
                attestation_token = None
                try:
                    from device_connect_container.security.attestation import AttestationTokenGenerator
                    generator = AttestationTokenGenerator(
                        device_id=self.device_id,
                        device_type=self.device_type,
                    )
                    attestation_token = generator.generate_token()
                    logger.info("Attestation token generated during commissioning")
                except ImportError:
                    logger.debug("Attestation skipped: device-connect-container not installed")
                except Exception as e:
                    logger.warning("Attestation token generation failed: %s", e)

                # Mark as commissioned
                self.commissioned = True
                received_credentials.update(credentials)
                if attestation_token:
                    received_credentials["attestation"] = attestation_token
                credentials_received.set()

                logger.info("Device commissioned successfully")

                return web.json_response({
                    'success': True,
                    'device_id': self.device_id,
                    'message': 'Device commissioned successfully'
                })

            except Exception as e:
                logger.error(f"Commissioning error: {e}")
                return web.json_response({
                    'success': False,
                    'error': str(e)
                }, status=500)

        async def handle_info(request):
            """Return device info for discovery."""
            info = {
                'device_id': self.device_id,
                'device_type': self.device_type,
                'capabilities': self.capabilities,
                'state': 'commissioned' if self.commissioned else 'uncommissioned',
                # 'qr_code_base64': self.generate_qr_code()
            }

            # Include NKey public key for JWT auth (never expose the private seed)
            if self.nkey_public:
                info['nkey_public'] = self.nkey_public

            return web.json_response(info)

        # Create web server
        app = web.Application()
        app.router.add_post('/commission', handle_commission)
        app.router.add_get('/info', handle_info)

        runner = web.AppRunner(app)
        await runner.setup()

        site = web.TCPSite(runner, '0.0.0.0', self.port)
        await site.start()

        logger.info(f"Commissioning server listening on port {self.port}")

        # Display QR code
        # print(self.show_qr_code_ascii())
        print(f"\n📱 Commissioning server ready at http://<device-ip>:{self.port}")
        print(f"   Run: devctl commission {self.device_id} --pin {self.factory_pin[:4]}-{self.factory_pin[4:]}")
        # print(f"   Or:  devctl commission {self.device_id} --qr-scan\n")

        # Wait for commissioning
        await credentials_received.wait()

        # Cleanup
        await runner.cleanup()

        return received_credentials

    def save_credentials(self, credentials: Dict[str, Any], path: str = "/credentials/device.creds"):
        """
        Save operational credentials to file.

        Args:
            credentials: Operational credentials from admin
            path: Path to save credentials file
        """
        creds_path = Path(path)
        creds_path.parent.mkdir(parents=True, exist_ok=True)

        with open(creds_path, 'w') as f:
            json.dump(credentials, f, indent=2)

        # Secure permissions
        creds_path.chmod(0o600)

        logger.info(f"Credentials saved to {path}")


def generate_factory_pin() -> str:
    """
    Generate cryptographically secure 8-digit PIN.

    Returns:
        8-digit PIN string
    """
    # Use secrets for cryptographic randomness
    # Generate number between 10000000 and 99999999
    pin_number = secrets.randbelow(90000000) + 10000000
    return str(pin_number)


def format_pin(pin: str) -> str:
    """
    Format PIN with separator for readability.

    Args:
        pin: 8-digit PIN

    Returns:
        Formatted PIN (e.g., "1234-5678")
    """
    if len(pin) != 8:
        raise ValueError("PIN must be 8 digits")

    return f"{pin[:4]}-{pin[4:]}"


def parse_pin(formatted_pin: str) -> str:
    """
    Parse formatted PIN back to 8-digit string.

    Args:
        formatted_pin: Formatted PIN (e.g., "1234-5678")

    Returns:
        8-digit PIN string
    """
    return formatted_pin.replace('-', '').replace(' ', '')

"""Security module for Device Connect.

This module provides security-related functionality including:
    - Access Control Lists (ACLs) for device-to-device authorization
    - Device commissioning with PIN-based onboarding
    - Credential loading and management

Components:
    - DeviceACL, FunctionACL, EventACL: ACL models
    - ACLMatcher, ACLManager: ACL enforcement
    - CommissioningMode: Device commissioning handler
    - CredentialsLoader: Credential file parsing

Example:
    from device_connect_server.security import (
        DeviceACL, ACLManager,
        CommissioningMode, generate_factory_pin,
        CredentialsLoader
    )

    # ACL management
    acl = DeviceACL(device_id="robot-001", visible_to=["orchestrator-*"])
    manager = ACLManager()
    manager.set_acl(acl)

    # Device commissioning
    pin = generate_factory_pin()
    comm = CommissioningMode(device_id="camera-001", device_type="camera", factory_pin=pin)

    # Credential loading
    creds = CredentialsLoader.load_from_file("/credentials/device.creds.json")
"""
from device_connect_server.security.acl import (
    DeviceACL,
    FunctionACL,
    EventACL,
    ACLMatcher,
    ACLManager,
)
from device_connect_server.security.credentials import CredentialsLoader

# Commissioning requires bcrypt (optional dependency).
# Import lazily so the rest of device_connect_server.security works without it.
try:
    from device_connect_server.security.commissioning import (
        CommissioningMode,
        CommissioningPIN,
        generate_factory_pin,
        format_pin,
        parse_pin,
    )
    _COMMISSIONING_AVAILABLE = True
except ImportError:
    _COMMISSIONING_AVAILABLE = False

__all__ = [
    # ACL
    "DeviceACL",
    "FunctionACL",
    "EventACL",
    "ACLMatcher",
    "ACLManager",
    # Credentials
    "CredentialsLoader",
]

if _COMMISSIONING_AVAILABLE:
    __all__ += [
        "CommissioningMode",
        "CommissioningPIN",
        "generate_factory_pin",
        "format_pin",
        "parse_pin",
    ]

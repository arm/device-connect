"""Armv9 security features for Device Connect containers.

Provides hardware-backed security for containerized capabilities:
- PSA Attestation: Verify device/container integrity before joining network
- Arm CCA Realms: Hardware-encrypted enclaves for sensitive capabilities
- MTE: Memory tagging for C/C++ memory safety
- OCI Image Signing: Cosign/Notation for container image integrity
"""

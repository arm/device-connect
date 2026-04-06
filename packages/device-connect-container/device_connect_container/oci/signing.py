"""OCI image signing convenience wrapper for the build pipeline.

Re-exports ImageSigner and ImageVerifier from the security module
for use in the OCI build pipeline (image_builder.py).

The actual implementation lives in security/image_signing.py to keep
all security-related code in one place. This module provides the
oci/-scoped import path referenced in the Phase 1 plan.

Usage:
    from device_connect_container.oci.signing import ImageSigner, ImageVerifier

    signer = ImageSigner()
    signer.sign_with_key("dc-cap-vision:latest", key_path="cosign.key")

    verifier = ImageVerifier()
    assert verifier.verify("dc-cap-vision:latest", public_key="cosign.pub")
"""

from device_connect_container.security.image_signing import (
    ImageSigner,
    ImageVerifier,
)

__all__ = ["ImageSigner", "ImageVerifier"]

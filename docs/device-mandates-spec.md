# Spec: Device Mandates

## Objective

Add an optional verifiable authorization layer for Device Connect RPC execution. A device function can declare that it requires a Device Mandate, and the runtime refuses to execute protected RPCs unless the caller presents a signed mandate that authorizes the target device, method, parameters, and validity window.

The first implementation slice proves the contract end to end with a lightweight HMAC-backed mandate format suitable for local tests and demos. The verifier is intentionally small and pluggable so a later slice can replace or augment the credential format with UCAN, Biscuit, or a standards-track profile without changing the decorator or RPC metadata contract.

## Commands

- Edge tests: `pytest packages/device-connect-edge/tests -q`
- Agent tools tests: `pytest packages/device-connect-agent-tools/tests -q`
- Focused mandate tests: `pytest packages/device-connect-edge/tests/test_mandate_verifier.py packages/device-connect-edge/tests/test_device_mandates.py packages/device-connect-agent-tools/tests/test_agent_mandates.py -q`

## Project Structure

- `packages/device-connect-edge/device_connect_edge/mandates.py`: mandate data helpers, signing, and verification.
- `packages/device-connect-edge/device_connect_edge/drivers/decorators.py`: `@requires_mandate` decorator metadata.
- `packages/device-connect-edge/device_connect_edge/device.py`: runtime enforcement before driver invocation.
- `packages/device-connect-edge/device_connect_edge/types.py`: function capability metadata for mandate requirements.
- `packages/device-connect-agent-tools/device_connect_agent_tools/tools.py`: pass mandate metadata through `_dc_meta`.

## Testing Strategy

Use test-driven slices:

- Pure unit tests for signing, verification, time windows, device/method binding, numeric constraints, tamper detection, and replay denial.
- Runtime tests for protected RPC denial before driver execution and successful execution with a valid mandate.
- Agent-tools tests that verify `invoke`, `invoke_many`, `broadcast`, and legacy `invoke_device` attach mandate data inside `_dc_meta`.

## Boundaries

- Always: fail closed for protected methods; keep mandate support optional for unprotected methods; preserve existing unprotected RPC behavior.
- Ask first: adding non-stdlib crypto/credential dependencies; changing transport protocols; adding persistent receipt storage; modifying CI.
- Never: treat unsigned client-provided mandate dictionaries as valid; pass `_dc_meta` into user driver methods; weaken existing ACL/TLS/JWT checks.

## Success Criteria

- A driver can mark an RPC with `@requires_mandate(scope="actuation")`.
- Discovery/capability metadata shows mandate requirements for protected functions.
- Direct JSON-RPC and broadcast execution reject protected functions with no mandate, invalid signature, wrong device, wrong method, expired mandate, or out-of-range parameters.
- Direct JSON-RPC and broadcast execution allow a protected function with a valid closed mandate.
- Agent tools can attach mandate data to invoke paths through `_dc_meta`.
- Existing unprotected RPC tests continue to pass.

## Open Questions

- Which production credential format should be the default: UCAN, Biscuit, or a future AP2-compatible non-payment profile?
- Where should production principal keys live: OS keystore, HSM/KMS, commissioning bundle, or registry-backed trust store?
- Should execution receipts be persisted first in the server state store or emitted as signed events before storage is added?
- Should replay protection be in-memory per device for v0, or backed by the server state layer for distributed deployments?

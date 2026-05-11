# Device Mandates

Device Mandates add a signed authorization envelope to sensitive RPCs. A driver marks a function with `@requires_mandate`, and the runtime denies that function unless the call includes a valid closed mandate in `_dc_meta.mandate`.

Use mandates for actuation that can affect safety, access, cost, or physical state. Read-only functions usually should not require mandates.

## Protect an RPC

Decorate the RPC with `@requires_mandate`. The decorator may be placed above or below `@rpc()`.

```python
from device_connect_edge.drivers import DeviceDriver, requires_mandate, rpc


class SmartLockDriver(DeviceDriver):
    device_type = "smart_lock"

    @requires_mandate(scope="actuation")
    @rpc()
    async def unlock(self, duration_s: int = 10) -> dict:
        return {"state": "unlocked", "duration_s": duration_s}
```

Discovery metadata for `unlock` includes:

```json
{"mandate": {"required": true, "scope": "actuation"}}
```

## Create Mandates

An open mandate is signed by the principal and delegates bounded authority to an agent. A closed mandate is signed by the agent for one concrete invocation.

```python
from datetime import datetime, timedelta, timezone

from device_connect_edge import create_closed_mandate, create_open_mandate

now = datetime.now(timezone.utc)
principal_key = b"principal-demo-key"
agent_key = b"agent-demo-key"

open_mandate = create_open_mandate(
    principal="operator",
    agent="agent-1",
    device_id="lock-front-door",
    methods=["unlock"],
    constraints={"duration_s": {"lte": 30}},
    not_before=now - timedelta(seconds=5),
    not_after=now + timedelta(minutes=5),
    key=principal_key,
)

closed_mandate = create_closed_mandate(
    open_mandate=open_mandate,
    agent="agent-1",
    device_id="lock-front-door",
    method="unlock",
    params={"duration_s": 20},
    key=agent_key,
    issued_at=now,
)
```

Pass the closed mandate through agent tools with the `mandate` argument:

```python
from device_connect_agent_tools import invoke

result = invoke(
    "device(lock-front-door).function(unlock)",
    params={"duration_s": 20},
    mandate=closed_mandate,
)
```

## Valid and Invalid Use Cases

Valid smart-lock use: unlock the front door for 20 seconds when the open mandate allows `unlock` on `lock-front-door` and constrains `duration_s <= 30`.

Invalid smart-lock use: reuse that same mandate for `duration_s=60`, another device, another method, or changed parameters. The signature and constraint checks fail closed before the driver method runs.

Valid heater use: set a room heater to 21.5 C when the open mandate allows `set_temperature` on `heater-living-room` and constrains `target_c` between 18 and 23.

Invalid heater use: request `target_c=28` or replay a previously used closed mandate nonce. The verifier denies the call.

See `packages/device-connect-edge/examples/device_mandates/mandate_examples.py` for runnable local examples of these cases.

## Testing Commands

Run the focused mandate tests:

```bash
pytest packages/device-connect-edge/tests/test_mandate_verifier.py packages/device-connect-edge/tests/test_device_mandates.py packages/device-connect-agent-tools/tests/test_agent_mandates.py -q
```

Run package test suites:

```bash
pytest packages/device-connect-edge/tests -q
pytest packages/device-connect-agent-tools/tests -q
```

Run the examples:

```bash
PYTHONPATH=packages/device-connect-edge python packages/device-connect-edge/examples/device_mandates/mandate_examples.py
```

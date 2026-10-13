<!-- SPDX-License-Identifier: Apache-2.0 -->
# device-connect-agent-tools-c

The **Device Connect agent-tools in C** — the external-agent side. A C analogue
of `device-connect-agent-tools`: the meta-tools an agent uses to discover and
drive a fleet through the portal HTTP API.

## Tools (`dc/agent_tools.h`)

- `dc_describe_fleet` -> `GET /api/agent/v1/fleet`
- `dc_list_devices(device_type, location)` -> `GET /api/agent/v1/devices`
- `dc_get_device_functions(device_id)` -> `GET /api/agent/v1/devices/{id}/functions`
- `dc_invoke_device(device_id, function, params, reason)` -> `POST /api/agent/v1/devices/{id}/invoke`

Each returns the parsed JSON response (caller `json_free`). HTTP + TLS via
libcurl (`dc/http.h`); JSON via the shared `dc/json.h`. Portal URL and token
come from `DEVICE_CONNECT_PORTAL_URL` / `DEVICE_CONNECT_PORTAL_TOKEN` (or the
`dc_agent` struct).

## Build

Requires libcurl.

```
make            # builds libdc_agent_tools.a + the dc_agent CLI
make test
```

## CLI

```
export DEVICE_CONNECT_PORTAL_URL=https://portal.deviceconnect.dev
export DEVICE_CONNECT_PORTAL_TOKEN=dcp_...
./dc_agent fleet
./dc_agent list temp_sensor
./dc_agent functions alpha-temp-001
./dc_agent invoke alpha-temp-001 set_target '{"celsius":42}' "daily setpoint"
```

Verified end-to-end against a live DC portal driving the C edge SDK
(`device-connect-edge-c`): discovered 3 devices and invoked them with the
device JSON-RPC responses (and `-32601`/`-32602` errors) propagating through
the portal envelope.

## Scope / not yet

- The four core meta-tools over the portal HTTP API. Event streaming
  (`/events/.../stream`) and the Strands/LangChain/MCP adapters from the Python
  package are not ported.

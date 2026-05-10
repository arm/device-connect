# Discovery

Device Connect uses one selector grammar to address devices, functions, and
events. The same selector string drives discovery: it tells the system
**which** entities you mean. Labels attached to devices, functions, and
events provide the dimensions to filter on.

This guide covers the labels schema, the selector grammar, and the two
tools that resolve selectors.

## Labels

Labels are key/value metadata. Values are strings or lists of strings.
Lists express composite identity (a smart camera that is both `camera` and
`inference`).

Drivers declare labels in two places:

```python
class SmartCamera(DeviceDriver):
    labels = {
        "category": ["camera", "inference"],
        "location": "lab-A/optics-bench",
    }

    @rpc(labels={"direction": "write", "modality": ["rgb", "4k"]})
    async def capture_image(self, resolution: str = "1080p") -> dict:
        ...

    @emit(labels={"modality": "motion"})
    async def state_change_detected(self, zone_id: str, state_class: str):
        ...
```

### Well-known keys

These keys carry conventional meaning. Custom keys are always allowed
alongside them.

| Question | Key | Applies to | Example values |
| --- | --- | --- | --- |
| What is it? | `category` | device | `camera`, `robot`, `hub`, `sensor`, `actuator`, `inference` |
| Where is it? | `location` | device | `lab-A`, `zone-A/dock` (`/`-hierarchical, glob-able) |
| Read or write? | `direction` | function (RPC) | `read`, `write` |
| Is it dangerous? | `safety` | function + event | `critical`, `informational` |
| What kind of signal? | `modality` | function + event | `rgb`, `thermal`, `infrared`, `motion`, `4k`, ... |

The RPC-vs-event distinction is structural (FunctionDef vs EventDef) and is
expressed by the selector scope, not by a label.

### Drivers without label declarations

Drivers that populate only the legacy `DeviceStatus.location` heartbeat
field are still discoverable by location: the value is mirrored into
`labels["location"]` at the discovery boundary so selector queries on
location work without a driver change.

## Selector grammar

```
device(<filters>)                            device-only
device(<filters>).function(<filters>)         RPCs on a device subset
device(<filters>).event(<filters>)            events on a device subset
function(<filters>)                           all RPCs across the fleet
event(<filters>)                              all events across the fleet
```

Inside `(...)`:

- `key:value` - single-value match
- `key:[v1,v2]` - OR within a key (matches if the label value contains any
  listed value; multi-valued labels match if any element is in the list)
- `key:pattern*` - anchored glob (`*`, `?`); `set_*` matches `set_threshold`
  but not `unset_threshold`. Use `*set*` for substring.
- `k1:v1,k2:v2` - AND across keys
- bare string (no colon) - id/name match: `device(robot-001)`,
  `function(capture_image)`. Globs allowed: `device(cam-*)`.
- `*` or empty - match all

Keys inside `device(...)` resolve against device labels; keys inside
`function(...)` resolve against function labels; keys inside `event(...)`
resolve against event labels. The `.` chains: "narrow to these devices,
then narrow to these functions or events on them."

### Case sensitivity

Selector matching is **case-sensitive** on both label keys and values, so
`device(category:Camera)` and `device(category:camera)` are not
equivalent. Kubernetes label selectors and AWS resource tag matching are
case-sensitive; we follow that convention. Use lowercase for label keys
and values as the convention in this repo; drivers in `tests/drivers/`
follow that.

### Selector examples

```
device(category:camera)                                       all cameras
device(category:[camera,robot], location:lab-A/*)             cameras or robots in lab-A
device(location:lab-A*)                                       lab-A and any descendant
device(*).function(direction:write, modality:rgb)             rgb-producing writes fleet-wide
device(*).event(modality:motion)                              all motion events
function(safety:critical)                                     critical RPCs fleet-wide
function(estop)                                               fleet emergency-stop targets
```

## Tools

### Discovery

#### `discover(selector, offset=0, limit=200)`

Resolves a selector to matched entities. Returns devices, function tuples,
or event tuples depending on the selector scope. The response includes a
`label_histogram` so you can see which dimensions to narrow on next without
a separate call.

`discover()` includes full schemas inline when the matched set is small,
and switches to a name-and-labels summary above
`DEVICE_CONNECT_FUNCTION_THRESHOLD` (default 20). The threshold is
configurable via environment variable.

#### `discover_labels(key=None, offset=0, limit=50)`

Returns the fleet label vocabulary. Use this first when you do not know
which dimensions are available.

- With no `key`: returns top values per key across each axis (`device_keys`,
  `function_keys`, `event_keys`).
- With a `key` like `"device.location"` or `"function.direction"`:
  paginates the full value list for that one key.

### Operations

Calling a function on devices is one logical operation; the only choice
is whether the caller waits for replies and how they arrive.

| Tool | Selector resolves to | Reply mode |
| --- | --- | --- |
| `invoke(selector, params)` | exactly one (device, function) tuple | sync, single result |
| `invoke_many(selector, params, timeout=)` | any number of (device, function) tuples | sync, aggregated |
| `broadcast(selector, params, where=, bindings=, fire_at=, on_late=)` | any number of (device, function) tuples | async; correlation-tagged replies stream as events |
| `subscribe(selector)` | events, or `"correlation:<id>"` for broadcast replies | live stream (`Subscription` handle) |
| `await_replies(correlation_id, timeout=, until=)` | replies for one broadcast | sync helper that subscribes, collects, returns |

`invoke_many` runs every target's call in parallel and returns when each
target has finished or hit its per-target timeout (30 s default). Partial
failures do not abort siblings; the response carries both `results` and
`errors` lists.

`broadcast` does the same fan-out asynchronously: the caller gets a
`correlation_id` immediately and replies stream back on a per-device
subject keyed by that id. Subscribe with `subscribe("correlation:<id>")`
or block with `await_replies(correlation_id, timeout=...)`.

### Edge-side `where` predicate

`broadcast` accepts an optional `where` expression that runs at each
candidate device. The predicate is a CEL (Common Expression Language)
string and sees four variables:

- `identity` — device-local identity dict (`device_id`, `device_type`, ...)
- `labels` — device labels (the same labels selectors filter on)
- `status` — device status (heartbeat-updated: `location`, `availability`,
  `battery`, `online`, ...)
- `bindings` — the shared payload passed to `broadcast` (selection masks,
  thresholds, lookup tables)

```python
broadcast(
    "device(category:camera).function(capture_image)",
    params={"resolution": "4k"},
    where="status.battery > 50 && labels.location == 'lab-A'",
)
```

The `where` predicate is sandboxed by CEL (no I/O, no filesystem). The
predicate evaluator is an optional install:

```
pip install device-connect-agent-tools[predicate]
```

Without the extra, calling `broadcast(..., where=...)` returns an
`invalid_predicate` error immediately at the dispatcher; calls without a
`where` work unchanged.

### Synchronized fan-out (`fire_at` + `on_late`)

`broadcast` accepts an optional `fire_at` (wall-clock epoch seconds).
Each device holds the message and fires from its own clock at the
deadline. `on_late` controls behaviour when a device receives the
message past the deadline:

- `"skip"` (default) — drop the call to preserve coherence.
- `"fire"` — execute immediately.

```python
broadcast(
    "device(category:phone).function(set_flashlight)",
    params={"on": True, "color": "white"},
    fire_at=time.time() + 0.500,    # 500 ms in the future
    on_late="skip",
)
```

With NTP-synced devices the achieved spread is typically 5-10 ms
(clock-sync residual) rather than the 50-150 ms a naive fire-on-receipt
broadcast would produce.

## Response envelopes

The three response shapes below — one for `discover`, two for
`discover_labels` — are the source of truth for callers. Fields not
listed are reserved for forward-compatible extensions; do not rely on
field order.

### `discover`

```json
{
  "scope": "device_only",
  "matched": 47,
  "returned": 20,
  "offset": 0,
  "next_offset": 20,
  "results": [...],
  "label_histogram": {
    "category": {
      "values": {"camera": 312, "robot": 89, "sensor": 601},
      "multivalued": true,
      "unique_devices": 1002
    }
  }
}
```

Fields:

- `scope` - one of `device_only`, `device_function`, `device_event`,
  `function_only`, `event_only`.
- `matched` - total matched entities (across all pages).
- `returned` - rows in this page.
- `offset` / `next_offset` - pagination cursor; `next_offset` is `null` when
  no more pages.
- `results` - per-page rows. Shape depends on scope (devices, function
  tuples, or event tuples).
- `label_histogram` - per-key vocabulary across the matched set
  (pre-pagination), so you can choose how to narrow next. On the device
  axis, multi-valued keys also carry `unique_devices`.

The hard ceiling on `limit` is 1000 to prevent runaway responses; ask for
more pages instead.

### `discover_labels` — multi-axis form (no `key`)

```json
{
  "total_devices": 1247,
  "total_functions": 7100,
  "total_events": 1292,
  "device_keys": {
    "category": {
      "values": {"camera": 312, "robot": 89, "sensor": 601},
      "multivalued": true,
      "unique_devices": 1002
    },
    "location": {
      "values": {"warehouse1/loading-dock": 120, "warehouse1/yard": 80, "lab-A/optics-bench": 45},
      "more": 1227
    }
  },
  "function_keys": {
    "direction": {"values": {"read": 4200, "write": 2900}}
  },
  "event_keys": {
    "modality": {"values": {"motion": 812, "thermal": 480}}
  }
}
```

Fields:

- `total_devices` / `total_functions` / `total_events` - fleet-wide entity
  counts on each axis.
- `device_keys` / `function_keys` / `event_keys` - per-axis vocabulary.
  Each value is a map of label key → entry, where each entry contains:
  - `values` - `{value: count}` map sorted by descending count, capped
    at the top-N most-frequent values per key (default `20`,
    configurable via `DEVICE_CONNECT_LABEL_VALUES_TOP_N`).
  - `more` - present and `> 0` iff the value list was cropped; the
    integer count of values omitted from this page. Omitted when no
    truncation occurred. To enumerate the full list, switch to the
    per-key form (`discover_labels(key="device.location")`).
  - `multivalued` - present and `true` iff at least one entity carries a
    list value for this key. Omitted when the key is single-valued
    everywhere on this axis.
  - `unique_devices` - device-axis only; the number of devices that
    carry this key at least once (deduplicates list values). Omitted on
    the function and event axes.

The same per-key entry shape is used inside `discover()`'s
`label_histogram`, including `more` truncation. Per-key
`discover_labels(key=...)` enumerates fully via its own pagination
cursor and is not truncated.

### `discover_labels` — per-key form (`key="device.location"`, etc.)

This form is paginated, not truncated: every distinct value is reachable
across pages via the `offset` / `next_offset` cursor. There is no
`more` field here; that field is specific to the multi-axis form above.

```json
{
  "axis": "device",
  "key": "location",
  "matched": 247,
  "returned": 50,
  "offset": 0,
  "next_offset": 50,
  "values": {"lab-A/optics-bench": 12, "lab-A/dock": 9, "warehouse1/yard": 8},
  "axis_total": 1247,
  "multivalued": true
}
```

Fields:

- `axis` - one of `"device"`, `"function"`, `"event"` (parsed from the
  dotted `key` argument).
- `key` - the label key without the axis prefix.
- `matched` - total distinct values for this key on this axis (across
  all pages).
- `returned` - values on this page.
- `offset` / `next_offset` - pagination cursor; `next_offset` is `null`
  when no more pages.
- `values` - `{value: count}` map for this page, sorted by descending
  count.
- `axis_total` - total entities on this axis (e.g., devices when
  `axis == "device"`); use as the denominator if you want coverage
  percentages.
- `multivalued` - present and `true` iff this key is multivalued on this
  axis. Omitted otherwise.

## Error responses

`discover` and `discover_labels` return errors as data inside the response
envelope rather than raising. The shape is stable so callers can branch on
the `code` programmatically and surface `message` to logs or users:

```json
{ "matched": 0, "returned": 0, "offset": 0, "next_offset": null,
  "results": [],
  "error": {
    "code": "selector_parse_error",
    "message": "Unknown scope 'widgets' at position 0\n  widgets(*)\n  ^"
  }
}
```

| Code | Cause |
| --- | --- |
| `invalid_selector` | Selector is not a string (or otherwise unusable as input) |
| `selector_parse_error` | Selector is a string but malformed |
| `connection_error` | Registry or messaging backend unavailable |
| `key_not_axis_qualified` | `discover_labels(key=...)` missing the `device.` / `function.` / `event.` prefix |
| `unknown_axis` | `discover_labels(key=...)` axis prefix not in `{device, function, event}` |

## Worked examples

### Browse the fleet vocabulary

```python
from device_connect_agent_tools import connect, discover_labels

connect()
vocab = discover_labels()
# {"total_devices": 1247, "total_functions": 7100, "total_events": 1292,
#  "device_keys":   {"category": {...}, "location": {...}},
#  "function_keys": {"direction": {...}, "modality": {...}, "safety": {...}},
#  "event_keys":    {"modality": {...}}}

# Drill into one dimension:
locations = discover_labels(key="device.location", limit=50)
```

### Find every camera in lab-A

```python
from device_connect_agent_tools import discover

result = discover("device(category:camera, location:lab-A/*)")
for d in result["results"]:
    print(d["device_id"], d["labels"])
```

### Find every write RPC on cameras, fleet-wide

```python
result = discover("device(category:camera).function(direction:write)")
for row in result["results"]:
    print(row["device_id"], row["name"])
```

### Paginate a large result set

```python
offset = 0
while True:
    page = discover("device(*)", offset=offset, limit=200)
    for d in page["results"]:
        process(d)
    if page["next_offset"] is None:
        break
    offset = page["next_offset"]
```

### Invoke a single function

```python
from device_connect_agent_tools import invoke

result = invoke(
    "device(robot-001).function(grip_close)",
    {"force_n": 10},
)
# {"success": True, "device_id": "robot-001", "function": "grip_close",
#  "result": {...}}
```

### Fan out across every camera in lab-A

```python
from device_connect_agent_tools import invoke_many

result = invoke_many(
    "device(category:camera, location:lab-A).function(capture_image)",
    {"resolution": "4k"},
)
# {"candidates": 12, "matched": 12, "succeeded": 12, "failed": 0,
#  "results": [...], "errors": []}
```

### Async fleet emergency stop

```python
from device_connect_agent_tools import broadcast, await_replies

result = broadcast("function(estop)")
# {"correlation_id": "br-7f3a91", "candidates": 240, ...}

replies = await_replies(result["correlation_id"], timeout=5.0)
# list of {device_id, success, result|error, actually_fired_at}
```

### Synchronized actuation across a phone fleet

```python
import time
from device_connect_agent_tools import broadcast

mask = build_mask_from_scores(threshold=0.8)  # caller-side selection
broadcast(
    "device(category:phone, location:auditorium-A).function(set_flashlight)",
    params={"on": True, "color": "white"},
    where="mask[seat_row][seat_col] == 1 && status.battery > 30",
    bindings={"mask": mask},
    fire_at=time.time() + 0.5,
    on_late="skip",
)
```

### Subscribe to motion events in lab-A

```python
from device_connect_agent_tools import subscribe

with subscribe("device(location:lab-A/*).event(modality:motion)") as sub:
    for event in sub.iter(timeout=60.0):
        handle(event)
```

## CLI

The same selector syntax drives the operator CLIs. Every CLI command
maps to the matching Python tool call.

```
# Discovery (devctl)
devctl discover "<selector>" [--offset N] [--limit M]
devctl discover-labels [--key K] [--offset N] [--limit M]

# Operations (statectl)
statectl invoke "<selector>" [--param k=v ...]
statectl invoke-many "<selector>" [--param k=v ...] [--timeout T]
statectl broadcast "<selector>" [--param k=v ...] [--where E]
                                [--bindings JSON] [--fire-at T]
                                [--on-late skip|fire]
statectl subscribe "<selector>" [--timeout T] [--until N]
statectl await <correlation_id> [--timeout T] [--until N]
```

`--param k=v` accepts JSON-shaped values (numbers, booleans, arrays,
objects); everything else passes through as a string. So
`--param resolution=4k` and `--param zones='[1,2,3]'` both work
without quoting heroics.

Each verb exits non-zero on tool-side errors so the verbs compose into
shell pipelines:

```
statectl broadcast "device(category:camera).function(capture_image)" \
    --param resolution=4k \
    | jq -r .correlation_id \
    | xargs statectl await --timeout 5
```

## Known limits

### Client-side filtering (v1)

`discover()` and `discover_labels()` currently load the full fleet via
`Connection.list_devices()` and apply the selector in-process. This is
fine at today's fleet sizes (low hundreds of devices) but does not scale
to the 10K-device worked example: the entire device list crosses the
wire on every call, regardless of how selective the selector is.

Push-down to the registry is intentionally deferred for v1 — the
selector grammar and response envelopes are designed so that swapping
the in-process filter for a registry-side query is a transparent
optimization, not a breaking change. Until then, callers running
against large fleets should:

- prefer `discover_labels(key=…)` over `discover()` when they only need
  vocabulary, and
- treat `discover("device(*)")` as an O(fleet) operation, not O(matched).

The operations layer should plan for push-down ahead of fleet growth
past ~1K devices.

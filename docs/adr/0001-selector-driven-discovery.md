# ADR 0001: Selector-driven discovery and operations

- **Status:** Accepted

## Summary

Device Connect exposes one selector grammar that addresses devices,
functions, and events. The same selector string drives every discovery and
operation tool: it tells the system **which** entities you mean. Labels
attached to devices, functions, and events provide the dimensions to filter
on.

Two reasons this matters in practice:

- **Agent context budgets.** Loading every device's full schema into an LLM
  context exhausts the budget on fleets of more than a few dozen devices.
  Selectors let an agent narrow first and load schemas only for what it
  actually needs.
- **Cross-cutting queries.** Real questions are rarely "list this one
  device" - they are "every camera in lab-A", "all critical RPCs",
  "any motion event in zone-B". One grammar covers all of them.

## Labels

Labels are key/value metadata on devices, functions, and events. Values are
strings or lists of strings. Lists express composite identity (a smart
camera that is both `camera` and `inference`).

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

| Question the agent asks | Key | Applies to | Example values |
| --- | --- | --- | --- |
| What is it? | `category` | device | `camera`, `robot`, `hub`, `sensor`, `actuator`, `inference` |
| Where is it? | `location` | device | `lab-A`, `zone-A/dock` (`/`-hierarchical, glob-able) |
| Read or write? | `direction` | function (RPC) | `read`, `write` |
| Is it dangerous? | `safety` | function + event | `critical`, `informational` |
| What kind of signal? | `modality` | function + event | `rgb`, `thermal`, `infrared`, `motion`, `4k`, ... |

The RPC-vs-event distinction is structural (FunctionDef vs EventDef) and is
expressed by the selector scope, not by a label.

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
  of the listed values; multi-valued labels match if any element is in the
  list)
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

### Examples

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

| Tool | What it returns |
| --- | --- |
| `discover_labels(key=None, offset=0, limit=50)` | Fleet label vocabulary. With no `key`, returns top values per key across each axis (device, function, event). With `key="device.location"` (etc.), paginates one key's values. Use this first when you do not know which dimensions are available. |
| `discover(selector, offset=0, limit=200)` | Resolves a selector to matched entities. Returns devices, function tuples, or event tuples depending on the selector scope. Includes a `label_histogram` so you can see which dimensions to narrow on next without a separate call. |

`discover()` includes full schemas inline when the matched set is small,
and switches to a name-and-labels summary above
`DEVICE_CONNECT_FUNCTION_THRESHOLD` (default 20). The threshold is
configurable.

### Operations

Calling a function on devices is one logical operation; the only choice is
whether you want to wait for replies and how they are surfaced.

| Tool | Selector resolves to | Reply mode |
| --- | --- | --- |
| `invoke(selector, params)` | exactly one RPC tuple | sync, single result |
| `invoke_many(selector, params, where=, bindings=)` | any number of RPC tuples | sync, aggregated |
| `broadcast(selector, function, params, where=, bindings=, fire_at=, on_late=)` | any number of RPC tuples | async; correlation-tagged replies stream as events |
| `subscribe(selector)` | events, or `correlation:<id>` for a broadcast's replies | subscription handle |
| `await_replies(correlation_id, timeout=, until=)` | replies for one broadcast | sync helper that subscribes, collects, returns |

`invoke_many` and `broadcast` accept an optional `where` predicate
evaluated at the edge against each candidate's identity, labels, and shared
`bindings`. Use `where` for self-knowable state ("battery > 50%") and
shared `bindings` for dispatcher-computed selection masks (spatial regions,
ML score top-K, random samples).

`broadcast` accepts `fire_at` (wall-clock epoch seconds) for synchronized
fan-out: each device holds the message and fires from its own clock at the
target time. `on_late` (`"skip"` or `"fire"`) controls behaviour when a
device receives the message after the deadline.

## Pagination

`discover` and `discover_labels` accept `offset` and `limit`. Responses
follow a stable envelope:

```json
{
  "matched": 7421,
  "returned": 200,
  "offset": 0,
  "next_offset": 200,
  "results": [...]
}
```

`next_offset` is `null` when there are no more pages. The hard ceiling on
`limit` is 1000 to prevent runaway responses; ask for more pages instead.

Operation tools (`invoke_many`, `broadcast`) do not paginate - that is a
streaming-dispatch concern. Subscribe to the result channel for per-target
detail at large fan-out.

## Worked examples

### Find every camera in lab-A and capture an image from each

```python
result = invoke_many(
    selector="device(category:camera, location:lab-A).function(capture_image)",
    params={"resolution": "1080p"},
)
# {"candidates": 12, "matched": 12, "succeeded": 12, "results": [...], "errors": []}
```

### Async fleet emergency-stop

```python
broadcast("function(estop)")
# {"correlation_id": "br-7f3a91", "candidates": 240}

# Optionally wait for replies:
replies = await_replies("br-7f3a91", timeout=5.0)
```

### Synchronized actuation across a phone fleet

```python
broadcast(
    selector="device(category:phone, location:auditorium-A)",
    function="set_flashlight",
    params={"on": True, "color": "white"},
    where="mask[seat_row][seat_col] == 1",
    bindings={"mask": <bitmap>},
    fire_at=time.time() + 0.500,
    on_late="skip",
)
```

### Browse the fleet vocabulary first

```python
vocab = discover_labels()
# {"total_devices": 1247, "total_functions": 7100,
#  "device_keys": {"category": {...}, "location": {...}},
#  "function_keys": {"direction": {...}, "modality": {...}, "safety": {...}},
#  "event_keys":   {"modality": {...}}}

# Then narrow to one dimension:
locations = discover_labels(key="device.location", limit=50)
```

### Subscribe to motion events in lab-A

```python
sub = subscribe("device(location:lab-A/*).event(modality:motion)")
# {"subscription_id": "sub-abc123", "matched": 8}
```

## CLI

The same selector syntax drives the operator CLIs. Every CLI command maps
to the matching tool call.

```
devctl discover-labels [--key K] [--offset N] [--limit M]
devctl discover "<selector>" [--offset N] [--limit M]

statectl invoke "<selector>" [--param k=v]
statectl invoke-many "<selector>" [--param k=v] [--where E]
statectl broadcast "<selector>" [--param k=v] [--where E] [--fire-at T]
statectl subscribe "<selector>"
statectl await "<correlation_id>" [--timeout T]
```

CLI flags `--param k=v` and `--where E` pack into the tool arguments; the
CLIs are thin shell wrappers over the Python tools.

# Atheris Fuzz Test Findings

Run date: 2026-04-06
Iterations: 10,000 per target
Platform: macOS arm64, Python 3.11.15, atheris 3.0.0

## Summary

| Target | Iterations | Crashes | Coverage |
|--------|-----------|---------|----------|
| fuzz_jsonrpc_cmd.py | 10,000 | 0 | 72 edges |
| fuzz_nats_creds.py | 10,000 | 0 | 8 edges |
| fuzz_pydantic_models.py | 10,000 | 0 | — |
| fuzz_credentials_json.py | ~900 | 1 | 35 edges |
| **Total** | **~30,900** | **1** | |

---

## Finding 1: TypeError crash in `_load_credentials_file`

**Severity**: Medium
**Target**: `fuzz_credentials_json.py`
**File**: `device_connect_edge/messaging/config.py:143`
**Crash artifact**: `crash-fe5dbbcea5ce7e2988b8c69bcfdfde8904aabc1f`

### Description

Atheris found this crash after ~900 iterations. When a credentials file contains
valid JSON that is not a dict (e.g., a bare integer), `_load_credentials_file()`
crashes with:

```
TypeError: argument of type 'int' is not iterable
```

at line 143: `if "nats" in data:`

### Reproducer

```
echo '8' > /tmp/bad_creds.json
python -c "
from device_connect_edge.messaging.config import MessagingConfig
MessagingConfig._load_credentials_file('/tmp/bad_creds.json')
"
```

Crash input (base64): `OA==` (the byte `0x38`, which is ASCII `8`)

### Root Cause

`json.load()` successfully parses `8` as an integer. The subsequent
`if "nats" in data` assumes `data` is a dict and fails because `in`
is not supported on `int`.

### Suggested Fix

```python
data = json.load(f)
if not isinstance(data, dict):
    return {}
```

---

## Notes

- The JSON-RPC command parser (`fuzz_jsonrpc_cmd.py`) showed no crashes across
  10,000 iterations. The W3C tracestate parser logged warnings for malformed
  identifiers but handled them gracefully.
- The NATS credentials parser (`fuzz_nats_creds.py`) showed no crashes. The
  `.find()` + slicing approach is tolerant of arbitrary input.
- The Pydantic model fuzzer (`fuzz_pydantic_models.py`) showed no crashes.
  Pydantic's validation layer correctly rejects or coerces all fuzzed input.
- For deeper coverage, run each target for longer (e.g., `-max_total_time=3600`
  for 1 hour per target).

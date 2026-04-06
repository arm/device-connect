# Hypothesis Fuzz Test Findings

Run date: 2026-04-06
Profile: default (5,000 examples per test)
Platform: macOS arm64, Python 3.11.15

## Summary

| Test File | Tests | Passed | Failed |
|-----------|-------|--------|--------|
| test_fuzz_credentials_json.py | 3 | 2 | 1 |
| test_fuzz_jsonrpc_cmd.py | 3 | 3 | 0 |
| test_fuzz_nats_creds.py | 2 | 1 | 1 |
| test_fuzz_pydantic_models.py | 3 | 3 | 0 |
| **Total** | **11** | **9** | **2** |

---

## Finding 1: TypeError in `_load_credentials_file` when JSON is not a dict

**Severity**: Medium
**Test**: `test_fuzz_credentials_json.py::test_load_credentials_raw_bytes_never_crashes`
**File**: `device_connect_edge/messaging/config.py:143`

### Description

`_load_credentials_file()` calls `json.load(f)` and then checks `if "nats" in data`.
When the JSON file contains a valid JSON value that is not a dict (e.g., an integer,
string, array, or boolean), the `in` operator raises:

```
TypeError: argument of type 'int' is not iterable
```

### Reproducer

A credentials file containing just `0`:

```
data=b'0'
```

### Root Cause

`json.load()` can return any JSON type (int, str, list, bool, None), not just dict.
The code assumes the result is always a dict and uses `if "nats" in data` without
checking the type first.

### Suggested Fix

Add a type guard before the `in` check:

```python
data = json.load(f)
if not isinstance(data, dict):
    return {}
```

---

## Finding 2: Carriage return handling in `_parse_nats_creds_file`

**Severity**: Low
**Test**: `test_fuzz_nats_creds.py::test_parse_nats_creds_roundtrip`
**File**: `device_connect_edge/messaging/config.py:183`

### Description

When a `.creds` file contains `\r` (carriage return) characters within the JWT or
NKey seed sections, the `.strip()` call on extracted content converts `\r` to `\n`,
altering the extracted value.

### Reproducer

A `.creds` file with content `0\r0` between the JWT markers:

```
content='0\r0'
```

The extracted JWT becomes `0\n0` instead of `0\r0`.

### Root Cause

Python's `str.strip()` removes all whitespace including `\r`. The content between
markers is extracted via string slicing and then `.strip()` is applied, which
normalizes line endings.

### Impact

Low in practice — real JWT tokens and NKey seeds are base64-encoded and don't
contain carriage returns. However, it indicates the parser doesn't preserve content
verbatim, which could matter for non-standard credential formats.

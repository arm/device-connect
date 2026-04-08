# Fuzz Testing for Device Connect Agent Tools

Fuzz tests for the agent-tools package using [Hypothesis](https://hypothesis.readthedocs.io/) (property-based, pytest-integrated) and [Atheris](https://github.com/google/atheris) (coverage-guided, libFuzzer-based).

For full setup instructions (installing atheris on macOS/Linux, deep fuzzing, CI integration), see the [edge fuzz README](../../device-connect-edge/fuzz/README.md).

## Fuzz Targets

| Target | Hypothesis | Atheris | What it tests |
|--------|-----------|---------|---------------|
| Tool Name Parsing | `test_fuzz_schema.py` | `fuzz_schema.py` | `parse_tool_name()` — MCP tool name splitting on `::` delimiter |
| JSON-RPC Parsing | `test_fuzz_jsonrpc_parsing.py` | `fuzz_jsonrpc_parsing.py` | Buffered message and event message parsing from `connection.py` |

## Running Locally

### Hypothesis (pytest)

```bash
cd packages/device-connect-agent-tools
pip install -e ".[dev,fuzz]"

# Run all fuzz tests — findings shown in terminal output
pytest fuzz/test_fuzz_*.py -v

# More examples for deeper coverage
HYPOTHESIS_PROFILE=ci pytest fuzz/test_fuzz_*.py -v
```

### Atheris

```bash
pip install atheris  # Linux: works directly. macOS: see edge README for LLVM setup.

# Run individual targets directly
cd packages/device-connect-agent-tools
python fuzz/fuzz_schema.py fuzz/corpus/tool_names/ -max_total_time=300
python fuzz/fuzz_jsonrpc_parsing.py fuzz/corpus/jsonrpc_messages/ -max_total_time=300

# Deep fuzzing (1 hour per target, run indefinitely with no flags)
python fuzz/fuzz_schema.py fuzz/corpus/tool_names/ -max_total_time=3600

# Run all targets across ALL packages (from repo root) — writes atheris-report.md
python packages/device-connect-edge/fuzz/run_atheris.py --iterations=50000
```

### Where to find results

| Tool | Where |
|------|-------|
| Hypothesis | Terminal output from pytest |
| Atheris (unified runner) | `atheris-report.md` at repo root |
| Atheris (direct) | `crash-<hash>` files in current directory |
| CI | GitHub Actions job summary (scroll down on run page) |

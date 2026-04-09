# Fuzz Testing for Device Connect Edge

This directory contains fuzz tests for the Device Connect Edge SDK using two complementary tools:

- **[Hypothesis](https://hypothesis.readthedocs.io/)** — Property-based fuzzing, integrated with pytest. Works on all platforms out of the box.
- **[Atheris](https://github.com/google/atheris)** — Coverage-guided fuzzing powered by libFuzzer. Best for deep, long-running fuzz campaigns.

## Fuzz Targets

| Target | Hypothesis (pytest) | Atheris | What it tests |
|--------|-------------------|---------|---------------|
| JSON-RPC commands | `test_fuzz_jsonrpc_cmd.py` | `fuzz_jsonrpc_cmd.py` | `DeviceRuntime._cmd_subscription()` parsing — JSON-RPC decoding, method/id/params extraction, `_dc_meta` trace context |
| NATS credentials | `test_fuzz_nats_creds.py` | `fuzz_nats_creds.py` | `MessagingConfig._parse_nats_creds_file()` — manual string parsing with `.find()` and slicing |
| Pydantic models | `test_fuzz_pydantic_models.py` | `fuzz_pydantic_models.py` | `DeviceIdentity`, `DeviceStatus`, `DeviceCapabilities`, `FunctionDef`, `EventDef` validation |
| Credentials JSON | `test_fuzz_credentials_json.py` | `fuzz_credentials_json.py` | `MessagingConfig._load_credentials_file()` — JSON credential loading with `.creds` fallback |

---

## Setup

### Prerequisites

- Python >= 3.11
- pip

### Install Hypothesis (all platforms)

```bash
cd packages/device-connect-edge
pip install -e ".[dev,fuzz]"
```

This installs `hypothesis` and `coverage`. You can now run all `test_fuzz_*.py` tests.

### Install Atheris

Atheris requires a Clang compiler with libFuzzer support. Setup differs by platform.

#### Linux

```bash
pip install atheris
```

On most Linux distributions, the system Clang includes libFuzzer and atheris installs directly.

If it fails, install a newer Clang:

```bash
# Ubuntu/Debian
sudo apt-get install clang

# Then retry
pip install atheris
```

#### macOS

Apple Clang does **not** include libFuzzer. You need LLVM from Homebrew:

```bash
# Step 1: Install LLVM (one-time, ~2-3 min via bottle)
brew install llvm

# Step 2: Install atheris using Homebrew's clang
CLANG_BIN="/opt/homebrew/opt/llvm/bin/clang" pip install atheris
```

> **Note**: If Homebrew LLVM doesn't include `libclang_rt.fuzzer_osx.a` (you can check
> with `find /opt/homebrew/opt/llvm -name "*fuzzer*"`), you'll need to build LLVM from
> source instead:
>
> ```bash
> git clone https://github.com/llvm/llvm-project.git
> cd llvm-project && mkdir build && cd build
> cmake -DLLVM_ENABLE_PROJECTS='clang;compiler-rt' -G "Unix Makefiles" ../llvm
> make -j $(sysctl -n hw.ncpu)
> CLANG_BIN="$(pwd)/bin/clang" pip install atheris
> ```
>
> The cloned `llvm-project/` directory can be deleted after atheris is installed.
> Nothing is installed system-wide; your system Clang and Xcode are unaffected.

#### Verify installation

```bash
python -c "import atheris; print('atheris', atheris.__version__, '- OK')"
```

---

## Running Hypothesis Tests (Recommended Starting Point)

Hypothesis tests run via pytest and work on any platform.

```bash
cd packages/device-connect-edge

# Run all fuzz tests
pytest tests/fuzz/test_fuzz_*.py -v

# Run a specific target
pytest tests/fuzz/test_fuzz_jsonrpc_cmd.py -v

# Reproducible run with a fixed seed
pytest tests/fuzz/test_fuzz_jsonrpc_cmd.py -v --hypothesis-seed=0

# Run more examples for deeper coverage
HYPOTHESIS_PROFILE=ci pytest tests/fuzz/test_fuzz_*.py -v
```

### Hypothesis Profiles

Two profiles are configured in `tests/fuzz/conftest.py`:

| Profile | Examples per test | Use case |
|---------|------------------|----------|
| `default` | 5,000 | Local development |
| `ci` | 20,000 | CI pipelines, thorough runs |

Select a profile with: `HYPOTHESIS_PROFILE=ci`

---

## Running Atheris (Deep Fuzzing)

Atheris is best for long-running, coverage-guided campaigns that discover deeper bugs.

```bash
cd packages/device-connect-edge

# Quick smoke test (1,000 iterations)
python tests/fuzz/fuzz_jsonrpc_cmd.py -atheris_runs=1000

# 5-minute run with seed corpus
python tests/fuzz/fuzz_jsonrpc_cmd.py tests/fuzz/corpus/jsonrpc_cmd/ -max_total_time=300

# Run all targets (5 min each)
python tests/fuzz/fuzz_jsonrpc_cmd.py tests/fuzz/corpus/jsonrpc_cmd/ -max_total_time=300
python tests/fuzz/fuzz_nats_creds.py tests/fuzz/corpus/nats_creds/ -max_total_time=300
python tests/fuzz/fuzz_pydantic_models.py tests/fuzz/corpus/pydantic_models/ -max_total_time=300
python tests/fuzz/fuzz_credentials_json.py tests/fuzz/corpus/credentials_json/ -max_total_time=300

# Run with coverage report
python -m coverage run tests/fuzz/fuzz_jsonrpc_cmd.py -atheris_runs=100000
python -m coverage html
open htmlcov/index.html
```

### Deep Fuzzing (recommended for thorough testing)

The commands above are quick smoke tests. For thorough bug discovery comparable to
an AFL campaign, run atheris for **hours** using `-max_total_time` (in seconds):

```bash
# 1 hour per target
python tests/fuzz/fuzz_jsonrpc_cmd.py tests/fuzz/corpus/jsonrpc_cmd/ -max_total_time=3600

# 8 hours overnight
python tests/fuzz/fuzz_credentials_json.py tests/fuzz/corpus/credentials_json/ -max_total_time=28800

# Run indefinitely until you Ctrl+C (like AFL)
python tests/fuzz/fuzz_jsonrpc_cmd.py tests/fuzz/corpus/jsonrpc_cmd/
```

**`-atheris_runs` vs `-max_total_time`**: `-atheris_runs=50000` caps iterations and
finishes in seconds — useful for CI gates. `-max_total_time=3600` runs for a fixed
duration regardless of iteration count — useful for deep fuzzing. Without either flag,
atheris runs **indefinitely** until interrupted, just like AFL.

**Why short runs find less**: Python fuzzing is slower than C-based AFL (~1,000–7,000
exec/s vs 10,000–50,000 exec/s). Short iteration-capped runs are smoke tests, not
deep campaigns. Run for hours on a dedicated machine for best results.

### Atheris Results

- **Crashes** are saved as `crash-<hash>` files in the current directory
- **Timeouts** are saved as `timeout-<hash>` files
- **Reproduce** a crash: `python tests/fuzz/fuzz_jsonrpc_cmd.py crash-<hash>`

### Auto-generated corpus cleanup

Atheris writes new corpus entries into the corpus directory as it discovers new
coverage paths. When using `tests/fuzz/run_atheris.py`, this is handled automatically —
seeds are copied into a temp directory that is cleaned up after each target runs.

When running atheris harnesses directly, pass a **temporary directory** instead of the
seed corpus to avoid polluting it:

```bash
# Copy seeds to a temp dir, run against it, then delete
cp -r tests/fuzz/corpus/jsonrpc_cmd/ /tmp/fuzz-corpus
python tests/fuzz/fuzz_jsonrpc_cmd.py /tmp/fuzz-corpus -max_total_time=300
rm -rf /tmp/fuzz-corpus
```

---

## Seed Corpus

The `tests/fuzz/corpus/` directory contains valid example inputs that atheris mutates to find edge cases:

```
tests/fuzz/corpus/
├── jsonrpc_cmd/       # Valid JSON-RPC command messages
├── nats_creds/        # NATS .creds file samples
├── pydantic_models/   # Valid Pydantic model JSON
└── credentials_json/  # JSON credentials files
```

Hypothesis generates its own inputs from strategies and does not use the seed corpus.

---

## Adding New Fuzz Targets

1. **Identify a parsing function** that processes external input (network messages, files, configs)
2. **Create a hypothesis test** in `tests/fuzz/test_fuzz_<name>.py`:
   ```python
   from hypothesis import given, settings
   from hypothesis import strategies as st

   @given(data=st.binary(max_size=4096))
   @settings(max_examples=5000)
   def test_target_never_crashes(data):
       try:
           your_parser(data)
       except (ExpectedException1, ExpectedException2):
           pass  # Expected rejections — not bugs
   ```
3. **Create an atheris harness** in `tests/fuzz/fuzz_<name>.py`:
   ```python
   import atheris, sys

   with atheris.instrument_imports():
       from your_module import your_parser

   def TestOneInput(data: bytes) -> None:
       try:
           your_parser(data)
       except (ExpectedException1, ExpectedException2):
           pass

   atheris.Setup(sys.argv, TestOneInput)
   atheris.Fuzz()
   ```
4. **Add seed inputs** to `tests/fuzz/corpus/<name>/`

### Guidelines

- Only catch **expected** exceptions — unexpected ones are bugs worth investigating
- Seed corpus should contain valid inputs; the fuzzers mutate them to find edge cases
- Use `FuzzedDataProvider` (atheris) or structured strategies (hypothesis) for typed input
- Run atheris for hours/days on a dedicated machine for best results

---

## CI Integration

Fuzz tests run automatically in GitHub Actions (`.github/workflows/ci.yml`) as two parallel jobs:

### `fuzz-tests-hypothesis`

- Runs on every push/PR to `main`
- Uses the `ci` profile (20,000 examples per test via `HYPOTHESIS_PROFILE=ci`)
- Runs all `tests/fuzz/test_fuzz_*.py` tests via pytest
- No special dependencies — works on `ubuntu-latest` out of the box

### `fuzz-tests-atheris`

- Runs on every push/PR to `main`
- Runs each atheris target for 50,000 iterations
- On `ubuntu-latest`, `pip install atheris` works directly (no LLVM setup needed)

Both jobs run in parallel with unit tests and do not block integration tests.

### Where findings are published

Findings from both tools are published to the **GitHub Actions job summary** — visible on
the Actions tab under each run's **Summary** section (scroll down past the job list).

**Hypothesis**: pytest produces a JUnit XML report, which `tests/fuzz/report_hypothesis.py` converts
to a markdown summary showing pass/fail counts and expandable tracebacks for each failure.
The JUnit XML is also uploaded as an artifact (retained 30 days).

**Atheris**: `tests/fuzz/run_atheris.py` runs all targets and generates `atheris-report.md` with a
results table and expandable crash details. Both the report and any `crash-*` files are
uploaded as artifacts (retained 30 days).

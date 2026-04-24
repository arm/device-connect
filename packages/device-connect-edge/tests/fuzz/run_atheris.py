# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Runner for all atheris fuzz targets with markdown report generation.

Runs each fuzz target across all packages, captures results, and writes
a single combined summary report to atheris-report.md.

Usage:
    python packages/device-connect-edge/tests/fuzz/run_atheris.py --iterations=50000
"""

import argparse
import glob
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

FUZZ_DIR = Path(__file__).parent
# Resolve to repo root (4 levels up from tests/fuzz/ inside a package)
REPO_ROOT = FUZZ_DIR.parent.parent.parent.parent

TARGETS = [
    # ── device-connect-edge ──
    {
        "name": "Edge: JSON-RPC Commands",
        "package": "packages/device-connect-edge",
        "script": "tests/fuzz/fuzz_jsonrpc_cmd.py",
        "corpus": "tests/fuzz/corpus/jsonrpc_cmd/",
    },
    {
        "name": "Edge: NATS Credentials",
        "package": "packages/device-connect-edge",
        "script": "tests/fuzz/fuzz_nats_creds.py",
        "corpus": "tests/fuzz/corpus/nats_creds/",
    },
    {
        "name": "Edge: Pydantic Models",
        "package": "packages/device-connect-edge",
        "script": "tests/fuzz/fuzz_pydantic_models.py",
        "corpus": "tests/fuzz/corpus/pydantic_models/",
    },
    {
        "name": "Edge: Credentials JSON",
        "package": "packages/device-connect-edge",
        "script": "tests/fuzz/fuzz_credentials_json.py",
        "corpus": "tests/fuzz/corpus/credentials_json/",
    },
    # ── device-connect-server ──
    {
        "name": "Server: Credentials Loader",
        "package": "packages/device-connect-server",
        "script": "tests/fuzz/fuzz_credentials.py",
        "corpus": "tests/fuzz/corpus/credentials_json/",
    },
    {
        "name": "Server: PIN Parsing",
        "package": "packages/device-connect-server",
        "script": "tests/fuzz/fuzz_commissioning.py",
        "corpus": "tests/fuzz/corpus/commissioning/",
    },
    # ── device-connect-agent-tools ──
    {
        "name": "Agent: Tool Name Parsing",
        "package": "packages/device-connect-agent-tools",
        "script": "tests/fuzz/fuzz_schema.py",
        "corpus": "tests/fuzz/corpus/tool_names/",
    },
    {
        "name": "Agent: JSON-RPC Parsing",
        "package": "packages/device-connect-agent-tools",
        "script": "tests/fuzz/fuzz_jsonrpc_parsing.py",
        "corpus": "tests/fuzz/corpus/jsonrpc_messages/",
    },
]


def run_target(target, iterations):
    """Run a single fuzz target. Returns dict with results.

    Uses a temporary directory for the live corpus so atheris doesn't
    write auto-generated entries into the seed corpus directory.
    Seeds are copied in, and the temp dir is cleaned up after the run.
    """
    pkg_dir = REPO_ROOT / target["package"]
    seed_dir = pkg_dir / target["corpus"]
    tmp_corpus = tempfile.mkdtemp(prefix=f"fuzz-corpus-{target['script'].split('/')[-1]}-")

    # Copy seed files into the temp corpus
    for f in seed_dir.iterdir():
        if f.is_file():
            shutil.copy2(f, tmp_corpus)

    cmd = [
        sys.executable,
        str(pkg_dir / target["script"]),
        tmp_corpus,
        f"-atheris_runs={iterations}",
    ]

    # Snapshot existing crash files before this target
    pre_crashes = set(glob.glob(str(REPO_ROOT / "crash-*")))

    start = time.time()
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    elapsed = time.time() - start

    # Clean up temp corpus
    shutil.rmtree(tmp_corpus, ignore_errors=True)

    # Only attribute NEW crash files to this target
    post_crashes = set(glob.glob(str(REPO_ROOT / "crash-*")))
    crashes = sorted(post_crashes - pre_crashes)

    return {
        "name": target["name"],
        "script": target["script"],
        "returncode": result.returncode,
        "elapsed": elapsed,
        "iterations": iterations,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "crashes": crashes,
    }


def extract_crash_info(stdout, stderr):
    """Extract crash details from atheris output.

    The Python traceback goes to stdout, while libFuzzer summary
    and artifact paths go to stderr.
    """
    combined = stdout + "\n" + stderr
    lines = combined.strip().split("\n")
    crash_lines = []
    capture = False
    for line in lines:
        if "Uncaught Python exception" in line:
            capture = True
        if capture:
            # Skip noisy instrumentation/libfuzzer info/stats lines
            if line.startswith(("INFO:", "WARNING:", "#")):
                continue
            crash_lines.append(line)
            if line.startswith("artifact_prefix"):
                break
    return "\n".join(crash_lines) if crash_lines else None


def generate_report(results, report_path):
    """Generate markdown report from all target results."""
    total_crashes = sum(len(r["crashes"]) for r in results)
    icon = "\u2705" if total_crashes == 0 else "\u274c"

    lines = []
    lines.append(f"## {icon} Atheris Fuzz Tests\n")
    lines.append("| Target | Iterations | Duration | Result |")
    lines.append("|--------|-----------|----------|--------|")

    for r in results:
        status = "\u274c Crash" if r["returncode"] != 0 else "\u2705 Clean"
        duration = f"{r['elapsed']:.1f}s"
        lines.append(f"| {r['name']} | {r['iterations']:,} | {duration} | {status} |")

    lines.append("")

    # Detail any crashes
    findings = [r for r in results if r["returncode"] != 0]
    if findings:
        lines.append("### Findings\n")
        for i, r in enumerate(findings, 1):
            lines.append(f"#### {i}. `{r['script']}`\n")

            crash_info = extract_crash_info(r["stdout"], r["stderr"])
            if crash_info:
                lines.append("<details><summary>Crash details</summary>\n")
                lines.append(f"```\n{crash_info}\n```\n")
                lines.append("</details>\n")

            if r["crashes"]:
                crash_files = ", ".join(f"`{Path(c).name}`" for c in r["crashes"])
                lines.append(f"**Crash artifacts**: {crash_files}\n")
    else:
        lines.append("> No findings — all fuzz targets completed cleanly.\n")

    report = "\n".join(lines)
    report_path.write_text(report)
    return report


def main():
    parser = argparse.ArgumentParser(description="Run atheris fuzz targets")
    parser.add_argument("--iterations", type=int, default=50000, help="Iterations per target")
    args = parser.parse_args()

    # Clean old crash files
    for f in glob.glob(str(REPO_ROOT / "crash-*")):
        Path(f).unlink()

    results = []
    exit_code = 0

    for target in TARGETS:
        print(f"Running {target['name']}...", flush=True)
        result = run_target(target, args.iterations)
        results.append(result)

        if result["returncode"] != 0:
            exit_code = 1
            print(f"  CRASH found in {target['name']}", flush=True)
        else:
            print(f"  Clean ({result['elapsed']:.1f}s)", flush=True)

    report_path = REPO_ROOT / "atheris-report.md"
    generate_report(results, report_path)
    print(f"\nReport written to {report_path}")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()

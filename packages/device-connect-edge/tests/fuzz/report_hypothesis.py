# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Parse JUnit XML from hypothesis fuzz tests and output a GitHub-flavored markdown summary.

Usage:
    python tests/fuzz/report_hypothesis.py fuzz-hypothesis-results.xml
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def main():
    if len(sys.argv) < 2:
        print("Usage: python report_hypothesis.py <junit-xml-path>", file=sys.stderr)
        sys.exit(1)

    xml_path = Path(sys.argv[1])
    if not xml_path.exists():
        print("## Hypothesis Fuzz Tests\n\n> No results file found.", file=sys.stderr)
        sys.exit(1)

    tree = ET.parse(xml_path)
    root = tree.getroot()

    # Collect stats
    total = 0
    passed = 0
    failed = 0
    errors = 0
    failures = []

    for suite in root.iter("testsuite"):
        for case in suite.iter("testcase"):
            total += 1
            failure_el = case.find("failure")
            error_el = case.find("error")
            if failure_el is not None:
                failed += 1
                failures.append({
                    "name": case.get("name", "unknown"),
                    "classname": case.get("classname", ""),
                    "message": failure_el.get("message", ""),
                    "text": (failure_el.text or "").strip(),
                })
            elif error_el is not None:
                errors += 1
                failures.append({
                    "name": case.get("name", "unknown"),
                    "classname": case.get("classname", ""),
                    "message": error_el.get("message", ""),
                    "text": (error_el.text or "").strip(),
                })
            else:
                passed += 1

    # Output markdown
    status = "pass" if failed == 0 and errors == 0 else "fail"
    icon = "\u2705" if status == "pass" else "\u274c"

    print(f"## {icon} Hypothesis Fuzz Tests\n")
    print("| Metric | Count |")
    print("|--------|-------|")
    print(f"| Total tests | {total} |")
    print(f"| Passed | {passed} |")
    print(f"| Failed | {failed} |")
    print(f"| Errors | {errors} |")
    print()

    if failures:
        print("### Findings\n")
        for i, f in enumerate(failures, 1):
            print(f"#### {i}. `{f['name']}`\n")
            if f["classname"]:
                print(f"**File**: `{f['classname']}`\n")
            if f["message"]:
                print(f"**Error**: `{f['message']}`\n")
            if f["text"]:
                # Truncate very long tracebacks
                text = f["text"]
                lines = text.split("\n")
                if len(lines) > 30:
                    text = "\n".join(lines[:25] + ["...", f"({len(lines) - 25} more lines)"])
                print(f"<details><summary>Traceback</summary>\n\n```\n{text}\n```\n\n</details>\n")
    else:
        print("> No findings — all fuzz tests passed.\n")


if __name__ == "__main__":
    main()

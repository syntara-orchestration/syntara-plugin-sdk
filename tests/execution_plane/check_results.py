"""Fail the workflow if EP's optional integration fixtures skipped a test."""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

EXPECTED_CASES = 7


def main() -> None:
    root = ET.parse(Path(sys.argv[1])).getroot()
    cases = root.findall(".//testcase")
    if len(cases) != EXPECTED_CASES or any(case.find("skipped") is not None for case in cases):
        raise SystemExit(
            f"Expected {EXPECTED_CASES} executed compatibility tests without skips"
        )


if __name__ == "__main__":
    main()

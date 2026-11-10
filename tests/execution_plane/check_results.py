"""Fail the workflow if EP's optional integration fixtures skipped a test."""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def main() -> None:
    root = ET.parse(Path(sys.argv[1])).getroot()
    cases = root.findall(".//testcase")
    if len(cases) != 3 or any(case.find("skipped") is not None for case in cases):
        raise SystemExit("Expected three executed compatibility tests without skips")


if __name__ == "__main__":
    main()

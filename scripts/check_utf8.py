"""Fail if any tracked text file is not valid UTF-8.

Home Assistant's marketplace reads repository files as UTF-8; one stray
latin-1 byte (e.g. a "³" saved with a Windows default encoding) breaks installs.

    python scripts/check_utf8.py
"""

from __future__ import annotations

import subprocess
import sys


def main() -> int:
    names = subprocess.run(
        ["git", "ls-files", "-z"], capture_output=True, check=True
    ).stdout.split(b"\0")
    bad = 0
    for raw in filter(None, names):
        name = raw.decode()
        with open(name, "rb") as handle:
            data = handle.read()
        if b"\0" in data:  # binary file (images etc.)
            continue
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as err:
            line = data.count(b"\n", 0, err.start) + 1
            print(f"{name}:{line}: not valid UTF-8 (byte 0x{data[err.start]:02x})")
            bad += 1
    print("all text files are valid UTF-8" if not bad else f"{bad} file(s) failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

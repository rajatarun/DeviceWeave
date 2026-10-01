#!/usr/bin/env python3
"""
Download the AutoTap Study 1 workbook into the git-ignored cache.

The file is fetched from the AutoTap artifact and is not committed.
AutoTap (Weijia He et al.; Lefan Zhang, Weijia He, Jesse Martinez, Noah
Brackenbury, Shan Lu, and Blase Ur), IEEE/ACM ICSE 2019,
https://ieeexplore.ieee.org/abstract/document/8811900.
Repository: https://github.com/zlfben/autotap (default branch master),
path data/Data - User Study 1.xlsx.

Licence: that repository's LICENSE is GPL-3.0. No separate licence was
found for the files in data/. DeviceWeave is Apache-2.0, so this script
only writes a local cache under .cache/autotap/ and the guard refuses any
other destination.

    python3 scripts/fetch_autotap_study1.py
    python3 scripts/fetch_autotap_study1.py --force
"""
from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path
from typing import Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from policy_authoring.compile_fidelity import (  # noqa: E402
    DatasetGuardError,
    assert_dataset_not_tracked,
    assert_safe_dataset_destination,
    study1_workbook_path,
)

STUDY1_URL = (
    "https://raw.githubusercontent.com/zlfben/autotap/master/"
    "data/Data%20-%20User%20Study%201.xlsx"
)


def fetch(url: str, dest: Path, timeout: float = 60.0) -> None:
    """Download ``url`` to ``dest`` after the destination has been checked."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".partial")
    assert_safe_dataset_destination(partial)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = response.read()
        partial.write_bytes(payload)
        partial.replace(dest)
    finally:
        if partial.exists():
            partial.unlink()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--dest",
        default=None,
        help="Cache path. Must stay under .cache/autotap/. "
             "Default: .cache/autotap/Data - User Study 1.xlsx.",
    )
    parser.add_argument("--url", default=STUDY1_URL, help="Workbook URL.")
    parser.add_argument("--force", action="store_true", help="Replace an existing cache file.")
    args = parser.parse_args(argv)

    dest = Path(args.dest) if args.dest else study1_workbook_path()
    try:
        assert_dataset_not_tracked()
        assert_safe_dataset_destination(dest)
    except DatasetGuardError as exc:
        print(f"fetch refused: {exc}", file=sys.stderr)
        return 2

    if dest.exists() and not args.force:
        print(f"already cached: {dest}")
        return 0

    try:
        fetch(args.url, dest)
    except DatasetGuardError as exc:
        print(f"fetch refused: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — surface download failures as a non-zero exit
        print(f"download failed: {exc}", file=sys.stderr)
        return 1
    print(f"cached {dest}")
    print("This file is git-ignored. Do not commit it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Generate or verify the desktop SQL mirror from the canonical DB schema.

``oculai-db/schema`` is the only hand-edited source.  The desktop resource
directory is a generated packaging mirror and CI uses ``--check`` to prevent
either missing migrations or independently edited SQL from shipping.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path


def _sql_files(root: Path) -> dict[Path, Path]:
    return {
        path.relative_to(root): path
        for path in root.rglob("*.sql")
        if path.is_file()
    }


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(source: Path, destination: Path) -> list[str]:
    source_files = _sql_files(source)
    destination_files = _sql_files(destination) if destination.exists() else {}
    errors: list[str] = []

    for relative in sorted(source_files.keys() - destination_files.keys()):
        errors.append(f"missing desktop schema resource: {relative.as_posix()}")
    for relative in sorted(destination_files.keys() - source_files.keys()):
        errors.append(f"unexpected desktop schema resource: {relative.as_posix()}")
    for relative in sorted(source_files.keys() & destination_files.keys()):
        if _digest(source_files[relative]) != _digest(destination_files[relative]):
            errors.append(f"schema resource drift: {relative.as_posix()}")
    return errors


def sync(source: Path, destination: Path) -> None:
    source_files = _sql_files(source)
    destination_files = _sql_files(destination) if destination.exists() else {}

    destination.mkdir(parents=True, exist_ok=True)
    for relative, source_path in source_files.items():
        destination_path = destination / relative
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, destination_path)

    for relative in destination_files.keys() - source_files.keys():
        (destination / relative).unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the generated mirror without modifying files",
    )
    args = parser.parse_args()

    repo = Path(__file__).resolve().parent.parent
    source = repo / "oculai-db" / "schema"
    destination = repo / "oculai-desktop" / "resources" / "schema"

    if args.check:
        errors = check(source, destination)
        if errors:
            print("DATABASE SCHEMA MIRROR DRIFT DETECTED:")
            for error in errors:
                print(f"  {error}")
            print("Run: python scripts/sync_db_schema.py")
            return 1
        print("OK: desktop SQL resources match canonical oculai-db/schema")
        return 0

    sync(source, destination)
    errors = check(source, destination)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("Synchronized desktop SQL resources from canonical oculai-db/schema")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

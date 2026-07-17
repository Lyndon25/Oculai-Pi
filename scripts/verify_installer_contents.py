#!/usr/bin/env python3
"""Audit an electron-builder unpacked directory for required Oculai runtimes."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--skip-database-smoke", action="store_true")
    args = parser.parse_args()
    app = args.app.resolve()
    resources = app / "resources"
    required = (
        resources / "app.asar",
        resources / "runtime" / "manifest.json",
        resources / "schema" / "001_extensions.sql",
        resources / "schema" / "008_seed.sql",
        resources / "pi-windows-x64" / "pi.exe",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        print("ERROR: installer content is incomplete:\n  " + "\n  ".join(missing), file=sys.stderr)
        return 1

    # app.asar is opaque to ordinary filesystem checks.  Inspect the exact
    # entries used by package.json and BrowserWindow so a path/layout mismatch
    # cannot ship as a blank Electron window.
    asar_check = """
const asar = require('@electron/asar');
const path = require('path');
const archive = process.argv[1];
const required = JSON.parse(process.argv[2]);
for (const entry of required) {
  try { asar.statFile(archive, entry.split('/').join(path.sep)); }
  catch (error) {
    console.error(`Missing app.asar entry: ${entry}: ${error.message}`);
    process.exit(1);
  }
}
"""
    asar_entries = (
        "dist/main/main/index.js",
        "dist/main/preload/index.cjs",
        "dist/renderer/index.html",
    )
    asar_result = subprocess.run(
        ["node", "-e", asar_check, str(resources / "app.asar"), json.dumps(asar_entries)],
        cwd=Path(__file__).resolve().parent.parent / "oculai-desktop",
        check=False,
    )
    if asar_result.returncode != 0:
        return asar_result.returncode

    verifier = Path(__file__).resolve().parent / "verify_runtime_bundle.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(verifier),
            "--root",
            str(resources / "runtime"),
            "--smoke-sidecar",
        ],
        check=False,
    )
    if completed.returncode != 0:
        return completed.returncode

    if not args.skip_database_smoke:
        database_smoke = Path(__file__).resolve().parent / "smoke_installer_database.py"
        completed = subprocess.run(
            [sys.executable, str(database_smoke), "--app", str(app)],
            check=False,
        )
        if completed.returncode != 0:
            return completed.returncode

    print(f"OK: installer resources validated at {resources}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

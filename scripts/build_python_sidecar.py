#!/usr/bin/env python3
"""Build the deterministic Oculai JSONL sidecar as a Windows executable."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "oculai-desktop" / "resources" / "runtime" / "python"
DEFAULT_WORK = ROOT / "work" / "runtime-build" / "python"
PYINSTALLER_VERSION = "6.15.0"
PYTHON_VERSION = (3, 12)


def _ensure_pyinstaller() -> None:
    if sys.version_info[:2] != PYTHON_VERSION:
        raise SystemExit(
            f"Python {PYTHON_VERSION[0]}.{PYTHON_VERSION[1]} is required for reproducible "
            f"sidecar builds; found {sys.version_info.major}.{sys.version_info.minor}."
        )
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    installed = result.stdout.strip()
    if result.returncode != 0 or installed != PYINSTALLER_VERSION:
        raise SystemExit(
            f"PyInstaller {PYINSTALLER_VERSION} is required (found {installed or 'none'}). "
            "Install the locked packaging dependencies from "
            "`oculai-mcp/requirements-runtime.lock`."
        )


def source_digest() -> str:
    source_root = ROOT / "oculai-mcp" / "src" / "oculai_mcp"
    sources = sorted(source_root.rglob("*.py"))
    sources.extend([
        ROOT / "oculai-mcp" / "src" / "oculai_mcp" / "tools_schema.json",
        ROOT / "oculai-mcp" / "pyproject.toml",
    ])
    digest = hashlib.sha256()
    for source in sources:
        digest.update(source.relative_to(ROOT).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(source.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def build(output_dir: Path, work_dir: Path, clean: bool) -> Path:
    _ensure_pyinstaller()
    output_dir = output_dir.resolve()
    work_dir = work_dir.resolve()

    allowed_output_root = (ROOT / "oculai-desktop" / "resources" / "runtime").resolve()
    allowed_work_root = (ROOT / "work" / "runtime-build").resolve()
    if not output_dir.is_relative_to(allowed_output_root):
        raise SystemExit(f"Refusing to write outside {allowed_output_root}: {output_dir}")
    if not work_dir.is_relative_to(allowed_work_root):
        raise SystemExit(f"Refusing to write outside {allowed_work_root}: {work_dir}")

    if clean:
        shutil.rmtree(work_dir, ignore_errors=True)
        shutil.rmtree(output_dir, ignore_errors=True)

    dist_dir = work_dir / "dist"
    spec_dir = work_dir / "spec"
    build_dir = work_dir / "build"
    for directory in (dist_dir, spec_dir, build_dir, output_dir):
        directory.mkdir(parents=True, exist_ok=True)

    entry = ROOT / "oculai-mcp" / "src" / "oculai_mcp" / "jsonl_server.py"
    source_root = ROOT / "oculai-mcp" / "src"
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        "oculai-sidecar",
        "--paths",
        str(source_root),
        "--collect-submodules",
        "oculai_mcp",
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(build_dir),
        "--specpath",
        str(spec_dir),
        str(entry),
    ]
    subprocess.run(command, cwd=ROOT, check=True)

    suffix = ".exe" if sys.platform == "win32" else ""
    built = dist_dir / f"oculai-sidecar{suffix}"
    if not built.is_file():
        raise SystemExit(f"PyInstaller reported success but did not create {built}")

    destination = output_dir / built.name
    shutil.copy2(built, destination)
    lock_file = ROOT / "oculai-mcp" / "requirements-runtime.lock"
    build_info = {
        "format_version": 1,
        "python_version": f"{PYTHON_VERSION[0]}.{PYTHON_VERSION[1]}",
        "pyinstaller_version": PYINSTALLER_VERSION,
        "requirements_lock_sha256": hashlib.sha256(lock_file.read_bytes()).hexdigest(),
        "source_sha256": source_digest(),
    }
    (output_dir / "build-info.json").write_text(
        json.dumps(build_info, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Sidecar written to {destination} ({destination.stat().st_size:,} bytes)")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--work", type=Path, default=DEFAULT_WORK)
    parser.add_argument("--no-clean", action="store_true")
    args = parser.parse_args()
    build(args.output, args.work, clean=not args.no_clean)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

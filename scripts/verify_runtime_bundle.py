#!/usr/bin/env python3
"""Validate and optionally manifest the self-contained desktop runtime bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from build_python_sidecar import source_digest


KEY_FILES = (
    "python/oculai-sidecar.exe",
    "python/build-info.json",
    "postgres/bin/pg_ctl.exe",
    "postgres/bin/initdb.exe",
    "postgres/bin/postgres.exe",
    "postgres/bin/psql.exe",
    "postgres/lib/vector.dll",
    "postgres/lib/pg_trgm.dll",
    "postgres/share/extension/vector.control",
    "postgres/share/extension/pg_trgm.control",
    "postgres/server_license.txt",
    "licenses/pgvector-LICENSE",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _version(executable: Path) -> str:
    completed = subprocess.run(
        [str(executable), "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"{executable.name} --version failed: {completed.stdout.strip()}")
    return completed.stdout.strip()


def _smoke_sidecar(executable: Path) -> dict[str, Any]:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    process = subprocess.Popen(
        [str(executable)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=creationflags,
    )
    deadline = time.monotonic() + 30
    ready: dict[str, Any] | None = None
    try:
        while time.monotonic() < deadline:
            line = process.stderr.readline() if process.stderr else ""
            if line:
                message = json.loads(line)
                if message.get("type") == "ready":
                    ready = message
                    break
            elif process.poll() is not None:
                break
        if ready is None:
            raise RuntimeError(f"Sidecar did not emit a ready message (exit={process.poll()})")
        if int(ready.get("tools", 0)) < 42:
            raise RuntimeError(f"Sidecar exposed only {ready.get('tools')} tools; expected at least 42")
        if not process.stdin or not process.stdout:
            raise RuntimeError("Sidecar stdio pipes were not created")
        request_id = "runtime-smoke-capabilities"
        process.stdin.write(json.dumps({
            "id": request_id,
            "method": "oculai_list_source_capabilities",
            "params": {},
        }) + "\n")
        process.stdin.flush()

        response_lines: queue.Queue[tuple[str | None, BaseException | None]] = queue.Queue(
            maxsize=1
        )

        def read_probe_response() -> None:
            try:
                response_lines.put((process.stdout.readline(), None))
            except BaseException as exc:
                response_lines.put((None, exc))

        threading.Thread(
            target=read_probe_response,
            daemon=True,
        ).start()
        try:
            response_line, response_error = response_lines.get(timeout=15)
        except queue.Empty as exc:
            raise RuntimeError("Sidecar accepted stdin but did not answer the probe tool call") from exc
        if response_error is not None:
            raise RuntimeError(f"Sidecar probe response was not valid UTF-8: {response_error}")
        if not response_line:
            raise RuntimeError("Sidecar stdout closed before the probe tool response")
        response = json.loads(response_line)
        sources = response.get("result", {}).get("sources", []) if response.get("ok") else []
        if response.get("id") != request_id or not isinstance(sources, list) or not sources:
            raise RuntimeError(f"Sidecar probe tool returned an invalid response: {response}")
        return {
            "verified": True,
            "tools": int(ready["tools"]),
            "probe": "oculai_list_source_capabilities",
            "source_count": len(sources),
        }
    finally:
        if process.stdin:
            process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        # A PyInstaller one-file executable uses a launcher/child process pair
        # on Windows.  The launcher can exit before its child after stdin is
        # closed, so waiting on ``process`` alone can leave the actual sidecar
        # alive and keep the runtime executable locked.  The ready frame is
        # emitted by that child and includes its real PID; always reap that
        # exact process tree as the final smoke-test cleanup step.
        if os.name == "nt" and ready is not None:
            subprocess.run(
                ["taskkill", "/PID", str(int(ready["pid"])), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                check=False,
                timeout=15,
            )


def validate(root: Path, smoke_sidecar: bool) -> dict[str, Any]:
    root = root.resolve()
    missing = [relative for relative in KEY_FILES if not (root / relative).is_file()]
    if missing:
        raise RuntimeError("Runtime bundle is incomplete; missing: " + ", ".join(missing))

    build_info = json.loads((root / "python/build-info.json").read_text("utf-8"))
    if build_info.get("python_version") != "3.12":
        raise RuntimeError(f"Unexpected sidecar Python version: {build_info.get('python_version')}")
    if build_info.get("pyinstaller_version") != "6.15.0":
        raise RuntimeError(
            f"Unexpected sidecar PyInstaller version: {build_info.get('pyinstaller_version')}"
        )
    lock_file = Path(__file__).resolve().parent.parent / "oculai-mcp/requirements-runtime.lock"
    if lock_file.is_file() and build_info.get("requirements_lock_sha256") != _sha256(lock_file):
        raise RuntimeError("Sidecar was not built from the current requirements-runtime.lock")
    current_source_digest = source_digest()
    if build_info.get("source_sha256") != current_source_digest:
        raise RuntimeError(
            "Sidecar was not built from the current Python source; rebuild the Windows runtime"
        )

    postgres_version = _version(root / "postgres/bin/postgres.exe")
    match = re.search(r"(\d+)(?:\.\d+)?", postgres_version)
    if not match or int(match.group(1)) < 16:
        raise RuntimeError(f"PostgreSQL 16+ is required, found: {postgres_version}")

    vector_sql = list((root / "postgres/share/extension").glob("vector--*.sql"))
    if not vector_sql:
        raise RuntimeError("pgvector SQL extension files are missing")
    trgm_sql = list((root / "postgres/share/extension").glob("pg_trgm--*.sql"))
    if not trgm_sql:
        raise RuntimeError("pg_trgm SQL extension files are missing")

    runtime_files = sorted(
        path for path in root.rglob("*") if path.is_file() and path.name != "manifest.json"
    )

    manifest: dict[str, Any] = {
        "format_version": 1,
        "platform": "win32-x64",
        "postgres_version": postgres_version,
        "psql_version": _version(root / "postgres/bin/psql.exe"),
        "sidecar_build": build_info,
        "key_files": {
            relative: {
                "size": (root / relative).stat().st_size,
                "sha256": _sha256(root / relative),
            }
            for relative in KEY_FILES
        },
        "files": {
            path.relative_to(root).as_posix(): {
                "size": path.stat().st_size,
                "sha256": _sha256(path),
            }
            for path in runtime_files
        },
        "postgres_file_count": sum(
            1 for path in (root / "postgres").rglob("*") if path.is_file()
        ),
    }
    if smoke_sidecar:
        manifest["sidecar_ready"] = _smoke_sidecar(root / "python/oculai-sidecar.exe")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--write-manifest", action="store_true")
    parser.add_argument("--smoke-sidecar", action="store_true")
    args = parser.parse_args()

    try:
        actual = validate(args.root, args.smoke_sidecar)
        manifest_path = args.root.resolve() / "manifest.json"
        if args.write_manifest:
            manifest_path.write_text(json.dumps(actual, indent=2, ensure_ascii=False) + "\n", "utf-8")
        elif not manifest_path.is_file():
            raise RuntimeError(f"Runtime manifest is missing: {manifest_path}")
        else:
            recorded = json.loads(manifest_path.read_text("utf-8"))
            for field in ("format_version", "platform", "postgres_version", "files"):
                if recorded.get(field) != actual.get(field):
                    raise RuntimeError(f"Runtime {field} does not match manifest.json")
        print(
            f"OK: runtime bundle ({actual['postgres_version']}, "
            f"{actual['postgres_file_count']} PostgreSQL files)"
        )
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

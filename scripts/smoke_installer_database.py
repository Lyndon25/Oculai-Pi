#!/usr/bin/env python3
"""Initialize and migrate a disposable database using only installer resources."""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parent.parent
WORK_ROOT = (ROOT / "work" / "installer-smoke").resolve()


def _run(
    command: list[str],
    env: dict[str, str],
    timeout: int = 90,
    *,
    capture_output: bool = True,
    input_text: str | None = None,
) -> str:
    stdout: int = subprocess.PIPE if capture_output else subprocess.DEVNULL
    completed = subprocess.run(
        command,
        env=env,
        stdout=stdout,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        input=input_text,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        rendered = subprocess.list2cmdline(command)
        raise RuntimeError(
            f"Command failed ({completed.returncode}): {rendered}\n{completed.stdout}"
        )
    return completed.stdout or ""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def smoke(app: Path) -> None:
    if os.name != "nt":
        raise RuntimeError("The installer database smoke test requires Windows")

    resources = app.resolve() / "resources"
    postgres = resources / "runtime" / "postgres"
    schema = resources / "schema"
    bin_dir = postgres / "bin"
    case_dir = (WORK_ROOT / str(uuid4())).resolve()
    if not case_dir.is_relative_to(WORK_ROOT):
        raise RuntimeError(f"Unsafe installer smoke directory: {case_dir}")
    data_dir = case_dir / "data"
    password_file = case_dir / "password.txt"
    log_file = case_dir / "postgres.log"
    password = secrets.token_urlsafe(32)
    port = _free_port()
    case_dir.mkdir(parents=True)
    password_file.write_text(password + "\n", encoding="utf-8")

    env = {
        **os.environ,
        "PATH": str(bin_dir),
        "PGPASSWORD": password,
        "PGCLIENTENCODING": "UTF8",
        "PGSHAREDIR": str(postgres / "share"),
    }
    pg_ctl = str(bin_dir / "pg_ctl.exe")
    psql = str(bin_dir / "psql.exe")
    started = False
    try:
        print("[installer-smoke] initializing bundled PostgreSQL", flush=True)
        _run([
            str(bin_dir / "initdb.exe"),
            "-D",
            str(data_dir),
            "-U",
            "oculai",
            "--pwfile",
            str(password_file),
            "--auth-host=scram-sha-256",
            "--auth-local=trust",
            "--encoding=UTF8",
            "--locale=C",
        ], env)
        password_file.unlink(missing_ok=True)

        print(f"[installer-smoke] starting disposable server on port {port}", flush=True)
        # Do not capture pg_ctl start's stdout in a pipe.  On Windows the
        # spawned postgres process can inherit that pipe handle, which keeps
        # subprocess.communicate() waiting for EOF for the lifetime of the
        # server even though pg_ctl itself has already exited.
        _run([
            pg_ctl,
            "start",
            "-D",
            str(data_dir),
            "-l",
            str(log_file),
            "-o",
            f"-p {port} -h 127.0.0.1",
            "-w",
        ], env, capture_output=False)
        started = True

        connection = ["-h", "127.0.0.1", "-p", str(port), "-U", "oculai"]
        print("[installer-smoke] creating application database", flush=True)
        _run([psql, *connection, "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-c",
              "CREATE DATABASE oculai"], env)
        baseline = sorted(path for path in schema.glob("0*.sql") if path.is_file())
        if not baseline:
            raise RuntimeError(f"No packaged baseline schema files found in {schema}")
        for sql_file in baseline:
            print(f"[installer-smoke] applying {sql_file.name}", flush=True)
            _run([psql, *connection, "-d", "oculai", "-v", "ON_ERROR_STOP=1", "-f",
                  str(sql_file)], env)

        migrations_dir = schema / "migrations"
        migrations = sorted(path for path in migrations_dir.glob("*.sql") if path.is_file())
        if migrations:
            _run([
                psql,
                *connection,
                "-d",
                "oculai",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    checksum TEXT,
                    description TEXT
                );
                ALTER TABLE schema_version ADD COLUMN IF NOT EXISTS checksum TEXT;
                ALTER TABLE schema_version ADD COLUMN IF NOT EXISTS description TEXT;
                """,
            ], env)
        for migration in migrations:
            version = migration.stem
            checksum = hashlib.sha256(migration.read_bytes()).hexdigest()
            sql = migration.read_text(encoding="utf-8")
            escaped_version = version.replace("'", "''")
            wrapped = f"""
                BEGIN;
                {sql}
                INSERT INTO schema_version (version, checksum, description)
                VALUES ('{escaped_version}', '{checksum}', 'Migration {escaped_version}')
                ON CONFLICT (version) DO UPDATE SET
                    applied_at = now(),
                    checksum = EXCLUDED.checksum,
                    description = EXCLUDED.description;
                COMMIT;
            """
            print(f"[installer-smoke] applying migration {migration.name}", flush=True)
            _run([
                psql,
                *connection,
                "-d",
                "oculai",
                "-v",
                "ON_ERROR_STOP=1",
                "-f",
                "-",
            ], env, input_text=wrapped)

        if migrations:
            migration_count = _run([
                psql,
                *connection,
                "-d",
                "oculai",
                "-tA",
                "-c",
                "SELECT count(*) FROM schema_version WHERE checksum IS NOT NULL",
            ], env)
            if int(migration_count.strip()) < len(migrations):
                raise RuntimeError(
                    f"Only {migration_count.strip()} of {len(migrations)} migrations were recorded"
                )

        print("[installer-smoke] verifying required extensions", flush=True)
        extensions = _run([
            psql,
            *connection,
            "-d",
            "oculai",
            "-tA",
            "-c",
            "SELECT extname FROM pg_extension WHERE extname IN ('vector','pg_trgm') ORDER BY 1",
        ], env)
        if extensions.strip().splitlines() != ["pg_trgm", "vector"]:
            raise RuntimeError(f"Required extensions were not installed: {extensions!r}")
        print(f"OK: packaged PostgreSQL initialized and migrated on disposable port {port}")
    finally:
        password_file.unlink(missing_ok=True)
        if started:
            print("[installer-smoke] stopping disposable server", flush=True)
            subprocess.run(
                [pg_ctl, "stop", "-D", str(data_dir), "-m", "fast", "-w"],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
        if case_dir.exists():
            if not case_dir.is_relative_to(WORK_ROOT):
                raise RuntimeError(f"Refusing to clean unsafe smoke directory: {case_dir}")
            shutil.rmtree(case_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    args = parser.parse_args()
    try:
        smoke(args.app)
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

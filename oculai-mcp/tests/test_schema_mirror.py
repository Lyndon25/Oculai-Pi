"""The desktop SQL tree is a generated mirror of the canonical DB schema."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_desktop_schema_mirror_has_no_drift() -> None:
    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, str(repo / "scripts" / "sync_db_schema.py"), "--check"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr

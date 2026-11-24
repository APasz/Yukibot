"""Execute the typed map client's DOM and failure-handling regression suite."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_map_client_behaviour() -> None:
    pnpm = shutil.which("pnpm")
    frontend = Path(__file__).resolve().parents[1] / "frontend"
    if pnpm is None or not (frontend / "node_modules").is_dir():
        pytest.skip("Install frontend development dependencies to check map client behaviour.")
    result = subprocess.run(
        [pnpm, "--dir", str(frontend), "test", "src/map"],
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

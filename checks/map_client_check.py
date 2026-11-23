"""Execute the map client's failure and freshness handling without a browser."""

import shutil
import subprocess
from pathlib import Path

import pytest

from web_dash.app_page import ModWebAppPageMixin


def test_map_client_behaviour() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for map client behaviour checks.")
    result = subprocess.run(
        [node, str(Path(__file__).parent / "fixtures" / "map_client_check.cjs")],
        input=ModWebAppPageMixin._map_client_script(),
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

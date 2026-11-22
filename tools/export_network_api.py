"""Export the diagnostics contracts without starting Yukibot or NiceGUI."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from web_api.network_models import NetworkReportInput  # noqa: E402
from web_api.network_routes import create_network_router  # noqa: E402


def main() -> None:
    application = FastAPI()
    application.include_router(create_network_router())
    destination = ROOT / "frontend" / "src" / "generated"
    destination.mkdir(parents=True, exist_ok=True)
    for name, schema in (
        ("openapi.json", application.openapi()),
        ("report.schema.json", NetworkReportInput.model_json_schema()),
    ):
        (destination / name).write_text(
            json.dumps(schema, indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()

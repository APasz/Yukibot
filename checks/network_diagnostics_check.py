from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError

from web_api.network_models import (
    MAX_REPORT_BYTES,
    NETWORK_LIMITS,
    FailedProbe,
    NetworkConfiguration,
    NetworkContext,
    NetworkIdentity,
    NetworkNodeContext,
    NetworkReportInput,
    NetworkTarget,
)
from web_api.network_reports import NetworkReportStore
from web_api.network_routes import NetworkBodyLimitMiddleware, create_network_router


class DiagnosticBackend:
    def __init__(self, directory: Path) -> None:
        self.network_reports = NetworkReportStore(directory)

    def network_identity(self, request: Request) -> NetworkIdentity:
        user_id = request.headers.get("x-test-user")
        if user_id is None:
            raise HTTPException(401, "Sign in required.")
        if user_id == "blocked":
            raise HTTPException(403, "Insufficient permissions.")
        return NetworkIdentity(
            user_id=user_id,
            display_name=f"Player {user_id}",
            is_admin=user_id == "admin",
        )

    def network_configuration(self, identity: NetworkIdentity) -> NetworkConfiguration:
        return NetworkConfiguration(
            identity=identity,
            targets=[
                NetworkTarget(
                    id="erin",
                    label="Erin",
                    url="https://example.invalid/api/node/ping",
                    kind="node",
                ),
            ],
            limits=NETWORK_LIMITS,
        )

    async def network_context(self) -> NetworkContext:
        return NetworkContext(observed_at_ms=1234, nodes=[])


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    backend = DiagnosticBackend(tmp_path / "reports")
    assets = tmp_path / "frontend"
    (assets / "assets").mkdir(parents=True)
    (assets / "index.html").write_text(
        '<html><script type="module" src="/mod-web/frontend/assets/app-hash.js"></script></html>'
    )
    (assets / "assets" / "app-hash.js").write_text("export const standalone = true;")
    (assets / "private.txt").write_text("not a public asset")
    application = FastAPI()
    application.add_middleware(NetworkBodyLimitMiddleware)
    application.include_router(create_network_router(backend, frontend_root=assets))
    with TestClient(application) as browser:
        yield browser
    backend.network_reports.close()


def recording(report_id: UUID | None = None) -> dict[str, object]:
    return {
        "id": str(report_id or uuid4()),
        "schema_version": 1,
        "started_at_ms": 1000000,
        "duration_ms": 10000,
        "title": "UL interaction delay",
        "notes": "Inventory opened slowly",
        "targets": [
            {
                "id": "erin",
                "label": "Erin",
                "url": "https://example.invalid/ping",
                "kind": "node",
            }
        ],
        "probes": [
            {
                "target_id": "erin",
                "offset_ms": 0,
                "hidden": False,
                "browser_delay_ms": 0,
                "status": "ok",
                "rtt_ms": 210,
            },
            {
                "target_id": "erin",
                "offset_ms": 2000,
                "hidden": False,
                "browser_delay_ms": 0,
                "status": "timeout",
            },
        ],
        "contexts": [{"offset_ms": 0, "snapshot": None, "error": "unavailable"}],
        "markers": [{"offset_ms": 4000, "label": "Container delayed"}],
    }


def test_page_is_standalone_and_uses_existing_login(client: TestClient) -> None:
    response = client.get("/mod-web/network", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login?next_path=%2Fmod-web%2Fnetwork"
    page = client.get("/mod-web/network", headers={"x-test-user": "one"})
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert "app-hash.js" in page.text
    assert "_nicegui" not in page.text and "socket.io" not in page.text
    assert (
        client.get("/mod-web/network", headers={"x-test-user": "blocked"}).status_code
        == 403
    )


def test_assets_are_immutable_and_cannot_escape_asset_directory(
    client: TestClient,
) -> None:
    asset = client.get("/mod-web/frontend/assets/app-hash.js")
    assert asset.status_code == 200
    assert "immutable" in asset.headers["cache-control"]
    assert (
        client.get("/mod-web/frontend/assets/%2e%2e%2fprivate.txt").status_code == 404
    )


@pytest.mark.parametrize(
    "path", ["/api/network/config", "/api/network/context", "/api/network/reports"]
)
def test_api_requires_authentication_and_does_not_cache_context(
    client: TestClient, path: str
) -> None:
    assert client.get(path).status_code == 401
    response = client.get(path, headers={"x-test-user": "one"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_reports_are_owned_and_admins_can_review_without_overwriting(
    client: TestClient,
) -> None:
    payload = recording()
    own = {"x-test-user": "one", "origin": "http://testserver"}
    response = client.post("/api/network/reports", headers=own, json=payload)
    assert response.status_code == 200, response.text
    report_id = response.json()["summary"]["id"]
    assert response.json()["summary"]["owner_id"] == "one"
    assert NetworkReportInput.model_validate_json(
        json.dumps(response.json()["recording"])
    ) == NetworkReportInput.model_validate_json(json.dumps(payload))
    # Saving again updates the same recording rather than duplicating a retried upload.
    assert (
        client.post("/api/network/reports", headers=own, json=payload).status_code
        == 200
    )
    assert len(client.get("/api/network/reports", headers=own).json()["reports"]) == 1
    other = {"x-test-user": "two"}
    assert client.get("/api/network/reports", headers=other).json()["reports"] == []
    assert (
        client.get(f"/api/network/reports/{report_id}", headers=other).status_code
        == 404
    )
    assert (
        client.post("/api/network/reports", headers=other, json=payload).status_code
        == 403
    )
    admin = {"x-test-user": "admin"}
    assert (
        client.get(f"/api/network/reports/{report_id}", headers=admin).status_code
        == 200
    )
    assert len(client.get("/api/network/reports", headers=admin).json()["reports"]) == 1
    assert (
        client.post("/api/network/reports", headers=admin, json=payload).status_code
        == 403
    )


def test_report_saves_reject_cross_origin_and_invalid_observations(
    client: TestClient,
) -> None:
    headers = {"x-test-user": "one", "origin": "https://another.invalid"}
    assert (
        client.post(
            "/api/network/reports", headers=headers, json=recording()
        ).status_code
        == 403
    )
    invalid = recording()
    invalid["owner_id"] = "admin"
    assert (
        client.post(
            "/api/network/reports", headers={"x-test-user": "one"}, json=invalid
        ).status_code
        == 422
    )
    invalid = recording()
    invalid["duration_ms"] = 1
    assert (
        client.post(
            "/api/network/reports", headers={"x-test-user": "one"}, json=invalid
        ).status_code
        == 422
    )


def test_body_limit_applies_without_content_length(client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        yield b'{"notes":"'
        yield b"x" * MAX_REPORT_BYTES
        yield b'"}'

    response = client.post(
        "/api/network/reports",
        headers={"x-test-user": "one", "content-type": "application/json"},
        content=chunks(),
    )
    assert response.status_code == 413


def test_report_validation_distinguishes_http_failures_and_unknown_targets() -> None:
    with pytest.raises(ValidationError):
        FailedProbe(
            target_id="erin",
            offset_ms=0,
            hidden=False,
            browser_delay_ms=0,
            status="http_error",
        )
    with pytest.raises(ValidationError):
        FailedProbe(
            target_id="erin",
            offset_ms=0,
            hidden=False,
            browser_delay_ms=0,
            status="timeout",
            http_status=503,
        )
    payload = recording()
    payload["targets"] = [
        {"id": "yuki", "label": "Yuki", "url": "/ping", "kind": "node"}
    ]
    with pytest.raises(ValidationError):
        NetworkReportInput.model_validate_json(json.dumps(payload))


def test_report_validation_rejects_impossible_observations() -> None:
    with pytest.raises(ValidationError, match="successful or informational"):
        FailedProbe(
            target_id="erin",
            offset_ms=0,
            hidden=False,
            browser_delay_ms=0,
            status="http_error",
            http_status=200,
        )
    for percent in (-1, 101):
        with pytest.raises(ValidationError):
            NetworkNodeContext(
                target_id="erin",
                availability="online",
                cpu_per_core_percent=[percent],
            )


def test_reports_survive_reopening_and_are_capped_per_player(tmp_path: Path) -> None:
    path = tmp_path / "reports"
    identity = NetworkIdentity(user_id="one", display_name="One", is_admin=False)
    store = NetworkReportStore(path)
    latest = None
    for _ in range(22):
        latest = store.save(
            NetworkReportInput.model_validate_json(json.dumps(recording())), identity
        )
    assert len(store.list(identity)) == 20
    assert latest is not None
    store.close()
    reopened = NetworkReportStore(path)
    assert reopened.get(latest.summary.id, identity) == latest
    reopened.close()


def test_new_api_import_does_not_import_nicegui() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from web_api.network_routes import create_network_router; assert 'nicegui' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

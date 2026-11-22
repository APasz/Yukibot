from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError

from node_api.process import NodeAppProcessSummary
from web_api.network_models import (
    MAX_REPORT_BYTES,
    DEFAULT_BANDWIDTH_SIZE,
    DEFAULT_BANDWIDTH_DURATION,
    NETWORK_LIMITS,
    FailedProbe,
    NetworkAppContext,
    NetworkAppList,
    NetworkBandwidthEndpoint,
    NetworkBandwidthSize,
    NetworkBandwidthDuration,
    NetworkConfiguration,
    NetworkContext,
    NetworkIdentity,
    NetworkMonitoredApp,
    NetworkNodeContext,
    NetworkReport,
    NetworkReportInput,
    NetworkTarget,
)
from web_api.network_bandwidth import NetworkBandwidthService
from web_api.network_reports import NetworkReportStore
from web_api.network_routes import NetworkBodyLimitMiddleware, create_network_router


class DiagnosticBackend:
    def __init__(self, directory: Path) -> None:
        self.network_reports = NetworkReportStore(directory)
        self.bandwidth = NetworkBandwidthService()

    async def network_bandwidth_session(
        self,
        request: Request,
        target_id: str,
        origin: str,
        size_bytes: NetworkBandwidthSize = DEFAULT_BANDWIDTH_SIZE,
        duration_ms: NetworkBandwidthDuration = DEFAULT_BANDWIDTH_DURATION,
    ) -> NetworkBandwidthEndpoint:
        identity = self.network_identity(request)
        if target_id != "erin":
            raise HTTPException(404, "Unknown bandwidth target.")
        return NetworkBandwidthEndpoint(
            target_id=target_id,
            url="https://example.invalid/api/node/bandwidth",
            grant=self.bandwidth.issue(
                actor_id=identity.user_id,
                origin=origin,
                size_bytes=size_bytes,
                duration_ms=duration_ms,
            ),
        )

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

    async def network_context(
        self,
        request: Request,
        node_name: str | None = None,
        app_name: str | None = None,
    ) -> NetworkContext:
        app = (
            None
            if node_name is None or app_name is None
            else NetworkAppContext(
                app=NetworkMonitoredApp(
                    target_id=node_name,
                    node_label="Erin",
                    app_name=app_name,
                    label="UL",
                ),
                process=NodeAppProcessSummary(
                    state="running",
                    captured_at_ms=1234,
                    cpu_percent=235.5,
                    memory_rss_bytes=1024,
                ),
                player_count=2,
                player_capacity=12,
            )
        )
        return NetworkContext(observed_at_ms=1234, nodes=[], app=app)

    async def network_apps(self) -> NetworkAppList:
        return NetworkAppList(
            apps=[
                NetworkMonitoredApp(
                    target_id="erin", node_label="Erin", app_name="ul", label="UL"
                )
            ],
            unavailable_nodes=[],
        )


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
    "path",
    [
        "/api/network/config",
        "/api/network/context",
        "/api/network/reports",
        "/api/network/apps",
    ],
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


def test_bandwidth_sessions_require_login_same_origin_and_a_known_target(
    client: TestClient,
) -> None:
    endpoint = "/api/network/bandwidth/erin"
    assert client.post(endpoint).status_code == 401
    headers = {"x-test-user": "one", "origin": "https://other.invalid"}
    assert client.post(endpoint, headers=headers).status_code == 403
    headers["origin"] = "http://testserver"
    assert (
        client.post("/api/network/bandwidth/missing", headers=headers).status_code
        == 404
    )
    response = client.post(endpoint, headers=headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["url"] == "https://example.invalid/api/node/bandwidth"
    assert (
        response.json()["grant"]["limits"]["transfer_duration_ms"]
        == DEFAULT_BANDWIDTH_DURATION.value
    )
    assert client.post(endpoint, headers=headers).status_code == 429


@pytest.mark.parametrize("size", list(NetworkBandwidthSize))
@pytest.mark.parametrize("duration", list(NetworkBandwidthDuration))
def test_bandwidth_api_accepts_supported_limit_combinations(
    client: TestClient, size: NetworkBandwidthSize, duration: NetworkBandwidthDuration
) -> None:
    response = client.post(
        "/api/network/bandwidth/erin",
        headers={"x-test-user": "one"},
        json={"size_bytes": size.value, "duration_ms": duration.value},
    )
    assert response.status_code == 200
    assert response.json()["grant"]["limits"]["max_bytes_per_direction"] == size.value
    assert response.json()["grant"]["limits"]["transfer_duration_ms"] == duration.value


@pytest.mark.parametrize("size", [0, -1, 123, 2**40, "bad", True, []])
def test_bandwidth_api_rejects_invalid_sizes(client: TestClient, size: object) -> None:
    assert (
        client.post(
            "/api/network/bandwidth/erin",
            headers={"x-test-user": "one"},
            json={"size_bytes": size},
        ).status_code
        == 422
    )


@pytest.mark.parametrize("duration", [0, -1, 4000, 30000, "bad", True, []])
def test_bandwidth_api_rejects_invalid_durations(
    client: TestClient, duration: object
) -> None:
    assert (
        client.post(
            "/api/network/bandwidth/erin",
            headers={"x-test-user": "one"},
            json={"duration_ms": duration},
        ).status_code
        == 422
    )


def test_speed_results_round_trip_without_credentials_and_old_reports_still_load(
    client: TestClient,
) -> None:
    data = recording()
    test = {
        "target": {
            "id": "portal",
            "label": "Portal",
            "url": "/mod-web/health",
            "kind": "portal",
        },
        "started_at_ms": 1001000,
        "max_bytes_per_direction": NetworkBandwidthSize.MIB_64.value,
        "transfer_duration_ms": NetworkBandwidthDuration.SECONDS_10.value,
        "duration_ms": 3000,
        "probes": [],
        "phases": [
            {
                "phase": phase,
                "offset_ms": index * 1000,
                "end_ms": (index + 1) * 1000,
                "transferred_bytes": 0 if index == 0 else 1000,
                "ended_by": "duration",
            }
            for index, phase in enumerate(("idle", "download", "upload"))
        ],
        "state": "completed",
        "error": None,
    }
    data["bandwidth_tests"] = [{"offset_ms": 1000, "test": test}]
    legacy = recording()
    legacy_test = {
        key: value
        for key, value in test.items()
        if key not in {"max_bytes_per_direction", "transfer_duration_ms"}
    }
    legacy["bandwidth_tests"] = [{"offset_ms": 1000, "test": legacy_test}]
    assert (
        NetworkReportInput.model_validate(legacy)
        .bandwidth_tests[0]
        .test.transfer_duration_ms
        == DEFAULT_BANDWIDTH_DURATION.value
    )
    assert (
        NetworkReportInput.model_validate(legacy)
        .bandwidth_tests[0]
        .test.max_bytes_per_direction
        == DEFAULT_BANDWIDTH_SIZE.value
    )
    test["phases"][1]["transferred_bytes"] = NetworkBandwidthSize.MIB_64.value
    response = client.post(
        "/api/network/reports", headers={"x-test-user": "one"}, json=data
    )
    assert response.status_code == 200, response.text
    saved = client.get(
        f"/api/network/reports/{data['id']}", headers={"x-test-user": "one"}
    ).json()["recording"]
    assert saved["bandwidth_tests"] == data["bandwidth_tests"]
    test["max_bytes_per_direction"] = DEFAULT_BANDWIDTH_SIZE.value
    assert (
        client.post(
            "/api/network/reports", headers={"x-test-user": "one"}, json=data
        ).status_code
        == 422
    )
    test["max_bytes_per_direction"] = NetworkBandwidthSize.MIB_64.value
    test["token"] = "secret"
    assert (
        client.post(
            "/api/network/reports", headers={"x-test-user": "one"}, json=data
        ).status_code
        == 422
    )
    del test["token"]
    test["duration_ms"] = 10000
    assert (
        client.post(
            "/api/network/reports", headers={"x-test-user": "one"}, json=data
        ).status_code
        == 422
    )
    assert NetworkReportInput.model_validate(recording()).bandwidth_tests == []


def test_selected_process_telemetry_round_trips_with_a_recording(
    client: TestClient,
) -> None:
    headers = {"x-test-user": "one"}
    app = client.get("/api/network/apps", headers=headers).json()["apps"][0]
    assert app["app_name"] == "ul"
    snapshot = client.get(
        "/api/network/context?node_name=erin&app_name=ul", headers=headers
    ).json()
    assert snapshot["app"]["process"]["cpu_percent"] == 235.5
    assert (
        client.get("/api/network/context?node_name=erin", headers=headers).status_code
        == 422
    )
    payload = recording()
    payload["monitored_app"] = app
    payload["contexts"] = [{"offset_ms": 1000, "snapshot": snapshot}]
    saved = client.post("/api/network/reports", headers=headers, json=payload)
    assert saved.status_code == 200, saved.text
    report_id = saved.json()["summary"]["id"]
    restored = client.get(f"/api/network/reports/{report_id}", headers=headers).json()[
        "recording"
    ]
    assert restored["monitored_app"] == app
    assert restored["contexts"][0]["snapshot"]["app"] == snapshot["app"]
    payload["monitored_app"] = {**app, "app_name": "different"}
    assert (
        client.post("/api/network/reports", headers=headers, json=payload).status_code
        == 422
    )


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


@pytest.mark.parametrize(
    "node_name,app_name",
    [("", "ul"), ("erin", ""), ("a" * 129, "ul"), ("erin", "a" * 257)],
)
def test_process_selection_rejects_invalid_identifiers(
    client: TestClient, node_name: str, app_name: str
) -> None:
    response = client.get(
        "/api/network/context",
        headers={"x-test-user": "one"},
        params={"node_name": node_name, "app_name": app_name},
    )
    assert response.status_code == 422


def test_report_library_and_saves_do_not_construct_other_recordings(
    tmp_path: Path,
) -> None:
    store = NetworkReportStore(tmp_path / "reports")
    identity = NetworkIdentity(user_id="one", display_name="One", is_admin=False)
    first = store.save(
        NetworkReportInput.model_validate_json(json.dumps(recording())), identity
    )
    try:
        with patch.object(
            NetworkReport,
            "model_validate_json",
            side_effect=AssertionError("Unexpected full recording load"),
        ):
            assert store.list(identity) == [first.summary]
            second = store.save(
                NetworkReportInput.model_validate_json(json.dumps(recording())),
                identity,
            )
            assert store.list(identity) == [second.summary, first.summary]
        assert store.get(first.summary.id, identity) == first
    finally:
        store.close()


def test_report_caps_preserve_newest_summaries_across_users(tmp_path: Path) -> None:
    store = NetworkReportStore(tmp_path / "reports")
    admin = NetworkIdentity(user_id="admin", display_name="Admin", is_admin=True)
    try:
        with (
            patch("web_api.network_reports.MAX_REPORTS_PER_USER", 2),
            patch("web_api.network_reports.MAX_STORED_REPORTS", 3),
        ):
            saved = [
                store.save(
                    NetworkReportInput.model_validate_json(json.dumps(recording())),
                    NetworkIdentity(user_id=owner, display_name=owner, is_admin=False),
                )
                for owner in ("one", "one", "one", "two", "three")
            ]
        assert store.list(admin) == [report.summary for report in reversed(saved[-3:])]
        assert store.get(saved[0].summary.id, admin) is None
        assert store.get(saved[1].summary.id, admin) is None
    finally:
        store.close()

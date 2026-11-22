from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from nicegui import ui
from nicegui.app.app import App

import config
from node_api.service import PortalNodeLatencyProbe
from node_api.system import NodeSystemSummary
from web_api.network_models import NetworkIdentity
from web_api.network_reports import NetworkReportStore
from web_dash.nicegui_protocols import ModWebFastApiApp, ModWebRouteUi
from web_dash.remote_node_monitor import (
    RemoteNodeAvailability,
    RemoteNodeMonitorSnapshot,
)
from web_dash.service import ModWebService
from web_dash.types import ModWebNodeLink


def links() -> tuple[ModWebNodeLink, ...]:
    return (
        ModWebNodeLink(
            node_name="portal",
            label="Portal",
            url="/",
            api_base_url="http://127.0.0.1:3180/api/node",
            api_url="/api/node-proxy/portal/apps",
            is_current=True,
            latency_probe_url="http://127.0.0.1:3180/api/node/ping",
        ),
        ModWebNodeLink(
            node_name="erin",
            label="Erin",
            url="/mod-web/nodes/erin",
            api_base_url="https://erin.example.invalid/api/node",
            api_url="/api/node-proxy/erin/apps",
            is_current=False,
            latency_probe_url="https://erin.example.invalid/api/node/ping",
        ),
    )


def test_portal_configuration_never_sends_browser_probes_to_its_loopback_api() -> None:
    service = ModWebService()
    identity = NetworkIdentity(user_id="one", display_name="One", is_admin=False)
    with (
        patch.object(
            config,
            "ACTIVE_BOT_PROFILE",
            SimpleNamespace(name=config.BotProfileName.PORTAL),
        ),
        patch.object(service, "_node_links", return_value=links()),
    ):
        configuration = service.network_configuration(identity)
    assert configuration.targets[0].kind == "portal"
    assert configuration.targets[0].url == "/mod-web/health"
    assert configuration.targets[1].url == "https://erin.example.invalid/api/node/ping"
    assert "/node-proxy/" not in configuration.targets[1].url


def test_context_preserves_capture_times_and_marks_retained_offline_metrics() -> None:
    service = ModWebService()
    nodes = links()
    summary = NodeSystemSummary(
        cpu_percent=42,
        ram_percent=65,
        ram_used_bytes=None,
        ram_total_bytes=None,
        storage_percent=None,
        storage_free_bytes=None,
        storage_total_bytes=None,
        cpu_per_core_percent=(5, 96),
        captured_at_epoch_seconds=100,
    )
    cached = RemoteNodeMonitorSnapshot(
        node=nodes[1],
        availability=RemoteNodeAvailability.OFFLINE,
        system_summary=summary,
    )
    probe = PortalNodeLatencyProbe(3, None, None, captured_at_epoch_ms=90000)
    with (
        patch.object(service, "_node_links", return_value=nodes),
        patch.object(service, "_remote_node_monitor_snapshot", return_value=cached),
        patch.object(
            service._node_api.system_monitoring, "build_summary", return_value=summary
        ),
        patch.object(
            service._node_api,
            "portal_node_latency_probes_async",
            AsyncMock(return_value={"erin": probe}),
        ),
    ):
        context = asyncio.run(service.network_context())
    node = context.nodes[1]
    assert node.availability == "offline"
    assert node.captured_at_ms == 100000
    assert node.cpu_per_core_percent == [5, 96]
    assert node.portal_probe_captured_at_ms == 90000
    assert node.portal_latency_ms == 3
    assert context.observed_at_ms > node.captured_at_ms


def test_remote_nodes_preserve_the_new_portal_owned_paths() -> None:
    service = ModWebService()
    with patch.object(
        config, "ACTIVE_BOT_PROFILE", SimpleNamespace(name=config.BotProfileName.ERIN)
    ):
        for path in ("/mod-web/network", "/mod-web/frontend/assets/app-hash.js"):
            assert service._remote_portal_path(path, target_node_name="erin") == path


def test_diagnostics_routes_work_through_the_registered_dashboard_stack(
    tmp_path: Path,
) -> None:
    service = ModWebService()
    application = App()
    identity = NetworkIdentity(user_id="one", display_name="One", is_admin=False)
    store = NetworkReportStore(tmp_path / "reports")
    with (
        patch.object(ui, "page", return_value=lambda route: route),
        patch.object(service, "_network_report_store", store),
        patch.object(service, "_remote_portal_redirect", return_value=None),
        patch.object(service, "_node_links", return_value=links()),
        patch.object(
            service, "network_identity", return_value=identity
        ) as authenticate,
    ):
        service._register_routes(
            nicegui_app=cast(ModWebFastApiApp, application),
            ui=cast(ModWebRouteUi, ui),
        )
        with TestClient(application) as client:
            response = client.get("/api/network/config")
            assert response.status_code == 200, response.text
            assert response.json()["identity"]["user_id"] == identity.user_id
            assert response.headers["cache-control"] == "no-store"
            page = client.get("/mod-web/network")
            assert page.status_code == 200
            assert "_nicegui" not in page.text and "socket.io" not in page.text
            authenticate.side_effect = HTTPException(401, "Sign in required.")
            assert client.get("/api/network/reports").status_code == 401
            assert (
                client.get("/mod-web/network", follow_redirects=False).status_code
                == 303
            )
    store.close()

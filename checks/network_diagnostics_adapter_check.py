from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from nicegui import ui
from nicegui.app.app import App

import config
from mod_web_auth import ModWebUser
from node_api.app_state import NodeAppEntry
from node_api.process import NodeAppProcessSummary
from node_api.service import PortalNodeLatencyProbe
from node_api.system import NodeSystemSummary
from node_auth import NodeApiScope
from web_api.network_models import NetworkContext, NetworkIdentity
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
        context = asyncio.run(service.network_context(Request({"type": "http"})))
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


def app_entry(*, name: str = "ul", running: bool = True) -> NodeAppEntry:
    return NodeAppEntry(
        name=name,
        friendly="Undead Legacy",
        node="erin",
        running=running,
        enabled=True,
        supports_mods=True,
        supports_configs=True,
        player_count=2,
        player_capacity=12,
    )


def test_app_discovery_lists_running_apps_and_excludes_retained_offline_state() -> None:
    service = ModWebService()
    nodes = links()
    snapshot = RemoteNodeMonitorSnapshot(
        node=nodes[1],
        availability=RemoteNodeAvailability.ONLINE,
        app_entries=(app_entry(), app_entry(name="stopped", running=False)),
    )
    with (
        patch.object(service, "_node_links", return_value=nodes),
        patch.object(service._node_api, "list_apps", AsyncMock(return_value=())),
        patch.object(
            service, "_remote_node_monitor_snapshot", return_value=snapshot
        ) as cached,
    ):
        result = asyncio.run(service.network_apps())
        assert [(app.target_id, app.app_name) for app in result.apps] == [
            ("erin", "ul")
        ]
        cached.return_value = RemoteNodeMonitorSnapshot(
            node=nodes[1],
            availability=RemoteNodeAvailability.OFFLINE,
            app_entries=snapshot.app_entries,
        )
        result = asyncio.run(service.network_apps())
        assert result.apps == [] and result.unavailable_nodes == ["Erin"]


def test_remote_process_keeps_player_credentials_and_host_context() -> None:
    service = ModWebService()
    nodes = links()
    user = ModWebUser(123, "Player", None, None)
    request = Request({"type": "http"})
    snapshot = RemoteNodeMonitorSnapshot(
        node=nodes[1],
        availability=RemoteNodeAvailability.ONLINE,
        app_entries=(app_entry(),),
    )
    process = NodeAppProcessSummary(
        state="running",
        captured_at_ms=100000,
        pid=1234,
        cpu_percent=175.5,
        memory_rss_bytes=4096,
    )
    with (
        patch.object(service, "_node_links", return_value=nodes),
        patch.object(service, "_require_http_user", return_value=user),
        patch.object(service, "_remote_node_monitor_snapshot", return_value=snapshot),
        patch.object(
            service,
            "_network_node_context",
            AsyncMock(return_value=NetworkContext(observed_at_ms=100000, nodes=[])),
        ),
        patch.object(
            service, "_remote_json_async", AsyncMock(return_value=process.model_dump())
        ) as remote,
    ):
        result = asyncio.run(service.network_context(request, "erin", "ul"))
        assert result.app is not None
        assert result.app.process.cpu_percent == 175.5 and result.app.player_count == 2
        assert remote.call_args.kwargs["user"] is user
        assert remote.call_args.kwargs["scopes"] == (NodeApiScope.APPS_READ,)
        remote.side_effect = RuntimeError("Remote node unavailable")
        unavailable = asyncio.run(service.network_context(request, "erin", "ul"))
        assert (
            unavailable.app is not None
            and unavailable.app.process.state == "unavailable"
        )
        assert unavailable.app.player_count is None
        assert unavailable.nodes == result.nodes


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


def test_failed_local_catalog_does_not_hide_remote_running_apps() -> None:
    service = ModWebService()
    nodes = links()
    remote = RemoteNodeMonitorSnapshot(
        node=nodes[1],
        availability=RemoteNodeAvailability.ONLINE,
        app_entries=(app_entry(),),
    )
    with (
        patch.object(service, "_node_links", return_value=nodes),
        patch.object(
            service._node_api,
            "list_apps",
            AsyncMock(side_effect=RuntimeError("Catalogue failed")),
        ),
        patch.object(service, "_remote_node_monitor_snapshot", return_value=remote),
    ):
        result = asyncio.run(service.network_apps())
    assert [(app.target_id, app.app_name) for app in result.apps] == [("erin", "ul")]
    assert result.unavailable_nodes == ["Portal"]


def test_slow_catalog_times_out_without_blocking_healthy_nodes() -> None:
    async def wait_for_catalog() -> tuple[NodeAppEntry, ...]:
        await asyncio.Event().wait()
        raise AssertionError("The pending catalogue should have been cancelled")

    service = ModWebService()
    nodes = links()
    remote = RemoteNodeMonitorSnapshot(
        node=nodes[1],
        availability=RemoteNodeAvailability.ONLINE,
        app_entries=(app_entry(),),
    )
    with (
        patch("web_dash.network_service._APP_TELEMETRY_TIMEOUT_SECONDS", 0.01),
        patch.object(service, "_node_links", return_value=nodes),
        patch.object(service._node_api, "list_apps", side_effect=wait_for_catalog),
        patch.object(service, "_remote_node_monitor_snapshot", return_value=remote),
    ):
        result = asyncio.run(service.network_apps())
    assert result.unavailable_nodes == ["Portal"]
    assert len(result.apps) == 1


def test_local_process_sampling_survives_a_failed_catalog() -> None:
    service = ModWebService()
    user = ModWebUser(123, "Player", None, None)
    process = NodeAppProcessSummary(
        state="running", captured_at_ms=100, pid=10, memory_rss_bytes=1024
    )
    with (
        patch.object(service, "_node_links", return_value=links()),
        patch.object(service, "_require_http_user", return_value=user),
        patch.object(
            service._node_api,
            "list_apps",
            AsyncMock(side_effect=RuntimeError("Catalogue failed")),
        ),
        patch.object(service, "_resolve_app"),
        patch.object(
            service._node_api.system_monitoring,
            "build_app_process_summary",
            return_value=process,
        ),
        patch.object(
            service,
            "_network_node_context",
            AsyncMock(return_value=NetworkContext(observed_at_ms=100, nodes=[])),
        ),
    ):
        result = asyncio.run(
            service.network_context(Request({"type": "http"}), "portal", "ul")
        )
    assert result.app is not None and result.app.process == process
    assert result.app.player_count is None


def test_unknown_node_is_rejected_before_starting_measurements() -> None:
    service = ModWebService()
    with (
        patch.object(service, "_node_links", return_value=links()),
        patch.object(service, "_require_http_user"),
        patch.object(service, "_network_node_context", AsyncMock()) as context,
        patch.object(service, "_network_app_context", AsyncMock()) as process,
    ):
        with pytest.raises(HTTPException) as error:
            asyncio.run(
                service.network_context(Request({"type": "http"}), "unknown", "ul")
            )
    assert error.value.status_code == 404
    context.assert_not_called()
    process.assert_not_called()

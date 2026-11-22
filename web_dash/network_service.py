"""Adapt the existing portal services to the standalone diagnostics API."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Final
from urllib.parse import quote, urlsplit, urlunsplit

from fastapi import HTTPException, Request

import config
from _async_utils import run_blocking
from _security import Power_Level
from mod_web_auth import ModWebUser
from node_api.app_state import NodeAppEntry
from node_api.process import NodeAppProcessSummary
from node_auth import NodeApiScope
from web_api.network_bandwidth import PORTAL_BANDWIDTH_PREFIX
from web_api.network_models import (
    NETWORK_LIMITS,
    DEFAULT_BANDWIDTH_SIZE,
    DEFAULT_BANDWIDTH_DURATION,
    NetworkAppContext,
    NetworkAppList,
    NetworkBandwidthEndpoint,
    NetworkBandwidthGrant,
    NetworkBandwidthSize,
    NetworkBandwidthDuration,
    NetworkConfiguration,
    NetworkContext,
    NetworkIdentity,
    NetworkMonitoredApp,
    NetworkNodeContext,
    NetworkTarget,
)
from web_api.network_reports import NetworkReportStore

from .constants import _PORTAL_HEALTH_PATH, log
from .remote_node_monitor import RemoteNodeAvailability
from .service_base import ModWebServiceSupport
from .types import ModWebNodeLink

_APP_TELEMETRY_TIMEOUT_SECONDS: Final[float] = 3.0


class ModWebNetworkMixin(ModWebServiceSupport):
    _network_report_store: NetworkReportStore | None = None

    @property
    def network_reports(self) -> NetworkReportStore:
        if self._network_report_store is None:
            self._network_report_store = NetworkReportStore(
                Path(".cache/network_diagnostics")
            )
        return self._network_report_store

    def close_network_reports(self) -> None:
        if self._network_report_store is not None:
            self._network_report_store.close()

    def network_identity(self, request: Request) -> NetworkIdentity:
        user = self._require_http_user(
            request=request, required_level=Power_Level.visitor
        )
        return NetworkIdentity(
            user_id=str(user.discord_id),
            display_name=self._web_display_name(user),
            is_admin=self._user_has_level(user, Power_Level.admin),
        )

    def network_configuration(self, identity: NetworkIdentity) -> NetworkConfiguration:
        targets: list[NetworkTarget] = []
        for node in self._node_links():
            is_portal = (
                node.is_current
                and config.ACTIVE_BOT_PROFILE.name is config.BotProfileName.PORTAL
            )
            # The current Portal node API address is loopback, never a browser destination.
            url = _PORTAL_HEALTH_PATH if is_portal else node.latency_probe_url
            if url is None:
                continue
            targets.append(
                NetworkTarget(
                    id=node.node_name,
                    label=node.label,
                    url=url,
                    kind="portal" if is_portal else "node",
                )
            )
        portal_host = urlsplit(config.MOD_WEB_SERVER.public_base_url).hostname
        portal_shares_host = portal_host is not None and any(
            target.kind == "node" and urlsplit(target.url).hostname == portal_host
            for target in targets
        )
        return NetworkConfiguration(
            identity=identity,
            targets=targets,
            bandwidth_target_ids=[
                target.id
                for target in targets
                if target.kind != "portal" or not portal_shares_host
            ],
            limits=NETWORK_LIMITS,
        )

    async def network_bandwidth_session(
        self,
        request: Request,
        target_id: str,
        origin: str,
        size_bytes: NetworkBandwidthSize = DEFAULT_BANDWIDTH_SIZE,
        duration_ms: NetworkBandwidthDuration = DEFAULT_BANDWIDTH_DURATION,
    ) -> NetworkBandwidthEndpoint:
        identity = self.network_identity(request)
        configuration = self.network_configuration(identity)
        target = next(
            (
                item
                for item in configuration.targets
                if item.id == target_id
                and item.id in configuration.bandwidth_target_ids
            ),
            None,
        )
        node = next(
            (item for item in self._node_links() if item.node_name == target_id), None
        )
        if target is None or node is None:
            raise HTTPException(404, "Unknown bandwidth target.")
        user = self._require_http_user(
            request=request, required_level=Power_Level.visitor
        )
        if node.is_current:
            grant = self._node_api.bandwidth.issue(
                actor_id=identity.user_id,
                origin=origin,
                size_bytes=size_bytes,
                duration_ms=duration_ms,
            )
        else:
            try:
                async with asyncio.timeout(_APP_TELEMETRY_TIMEOUT_SECONDS):
                    payload = await self._remote_json_async(
                        node=node,
                        app_name=None,
                        path="/bandwidth/sessions",
                        scopes=(NodeApiScope.APPS_READ,),
                        user=user,
                        method="POST",
                        json_payload={
                            "origin": origin,
                            "size_bytes": size_bytes.value,
                            "duration_ms": duration_ms.value,
                        },
                        timeout=_APP_TELEMETRY_TIMEOUT_SECONDS,
                    )
                grant = NetworkBandwidthGrant.model_validate(payload)
            except HTTPException as error:
                if error.status_code in {404, 422}:
                    raise HTTPException(
                        503, "Update this node to enable bandwidth tests."
                    ) from error
                raise
            except (OSError, TimeoutError, ValueError, RuntimeError) as error:
                raise HTTPException(
                    503, "Bandwidth test unavailable on this node."
                ) from error
        if (
            grant.limits.max_bytes_per_direction != size_bytes.value
            or grant.limits.transfer_duration_ms != duration_ms.value
        ):
            raise HTTPException(
                503, "This node did not accept the selected test limits."
            )
        if target.kind == "portal":
            url = PORTAL_BANDWIDTH_PREFIX
        else:
            parsed = urlsplit(target.url)
            url = urlunsplit(
                (
                    parsed.scheme,
                    parsed.netloc,
                    parsed.path.rsplit("/", 1)[0] + "/bandwidth",
                    "",
                    "",
                )
            )
        return NetworkBandwidthEndpoint(target_id=target_id, url=url, grant=grant)

    async def _network_app_entries(
        self, node: ModWebNodeLink
    ) -> tuple[NodeAppEntry, ...] | None:
        try:
            if node.is_current:
                async with asyncio.timeout(_APP_TELEMETRY_TIMEOUT_SECONDS):
                    return await self._node_api.list_apps()
            snapshot = self._remote_node_monitor_snapshot(node=node)
            return (
                snapshot.app_entries
                if snapshot.availability is RemoteNodeAvailability.ONLINE
                else None
            )
        except (HTTPException, OSError, TimeoutError, RuntimeError) as error:
            log.warning(
                "Diagnostics app list unavailable: node=%s error=%s: %s",
                node.node_name,
                type(error).__name__,
                error,
            )
            return None

    @staticmethod
    def _network_app(node: ModWebNodeLink, entry: NodeAppEntry) -> NetworkMonitoredApp:
        return NetworkMonitoredApp(
            target_id=node.node_name,
            node_label=node.label,
            app_name=entry.name,
            label=entry.friendly,
        )

    async def network_apps(self) -> NetworkAppList:
        apps: list[NetworkMonitoredApp] = []
        unavailable: list[str] = []
        for node in self._node_links():
            entries = await self._network_app_entries(node)
            if entries is None:
                unavailable.append(node.label)
            else:
                apps.extend(
                    self._network_app(node, entry) for entry in entries if entry.running
                )
        return NetworkAppList(apps=apps, unavailable_nodes=unavailable)

    async def _network_app_context(
        self, node: ModWebNodeLink, app_name: str, user: ModWebUser
    ) -> NetworkAppContext:
        entries = await self._network_app_entries(node)
        entry = next((entry for entry in entries or () if entry.name == app_name), None)
        app = (
            self._network_app(node, entry)
            if entry is not None
            else NetworkMonitoredApp(
                target_id=node.node_name,
                node_label=node.label,
                app_name=app_name,
                label=app_name,
            )
        )
        try:
            if node.is_current:
                process = await run_blocking(
                    self._node_api.system_monitoring.build_app_process_summary,
                    self._resolve_app(app_name),
                )
            else:
                async with asyncio.timeout(_APP_TELEMETRY_TIMEOUT_SECONDS):
                    payload = await self._remote_json_async(
                        node=node,
                        app_name=app_name,
                        path=f"/apps/{quote(app_name, safe='')}/process",
                        scopes=(NodeApiScope.APPS_READ,),
                        user=user,
                        timeout=_APP_TELEMETRY_TIMEOUT_SECONDS,
                    )
                process = NodeAppProcessSummary.model_validate(payload)
        except (
            HTTPException,
            OSError,
            TimeoutError,
            ValueError,
            RuntimeError,
        ) as error:
            log.warning(
                "Diagnostics process telemetry unavailable: node=%s app=%s error=%s",
                node.node_name,
                app_name,
                error,
            )
            process = NodeAppProcessSummary(
                state="unavailable", captured_at_ms=round(time.time() * 1000)
            )
        return NetworkAppContext(
            app=app,
            process=process,
            player_count=entry.player_count
            if entry is not None and process.state == "running"
            else None,
            player_capacity=entry.player_capacity
            if entry is not None and process.state == "running"
            else None,
        )

    async def network_context(
        self,
        request: Request,
        node_name: str | None = None,
        app_name: str | None = None,
    ) -> NetworkContext:
        if (node_name is None) != (app_name is None):
            raise ValueError("Choose both a node and an app.")
        if node_name is None or app_name is None:
            return await self._network_node_context()
        node = next(
            (node for node in self._node_links() if node.node_name == node_name), None
        )
        if node is None:
            raise HTTPException(404, "Unknown monitored node.")
        user = self._require_http_user(
            request=request, required_level=Power_Level.visitor
        )
        context, app = await asyncio.gather(
            self._network_node_context(),
            self._network_app_context(node, app_name, user),
        )
        return NetworkContext(
            observed_at_ms=round(time.time() * 1000), nodes=context.nodes, app=app
        )

    async def _network_node_context(self) -> NetworkContext:
        observations: list[NetworkNodeContext] = []
        probes = await self._node_api.portal_node_latency_probes_async()
        for node in self._node_links():
            if node.is_current:
                summary = await run_blocking(
                    self._node_api.system_monitoring.build_summary
                )
                availability = "online"
            else:
                snapshot = self._remote_node_monitor_snapshot(node=node)
                summary = snapshot.system_summary
                availability = (
                    "online"
                    if snapshot.availability is RemoteNodeAvailability.ONLINE
                    else "offline"
                    if snapshot.availability is RemoteNodeAvailability.OFFLINE
                    else "connecting"
                )
            probe = probes.get(node.node_name)
            captured_at = None if summary is None else summary.captured_at_epoch_seconds
            observations.append(
                NetworkNodeContext(
                    target_id=node.node_name,
                    availability=availability,
                    captured_at_ms=None if captured_at is None else captured_at * 1000,
                    cpu_percent=None if summary is None else summary.cpu_percent,
                    cpu_per_core_percent=[]
                    if summary is None
                    else list(summary.cpu_per_core_percent),
                    ram_percent=None if summary is None else summary.ram_percent,
                    portal_latency_ms=None if probe is None else probe.latency_ms,
                    portal_probe_captured_at_ms=None
                    if probe is None
                    else probe.captured_at_epoch_ms,
                )
            )
        return NetworkContext(
            observed_at_ms=round(time.time() * 1000), nodes=observations
        )

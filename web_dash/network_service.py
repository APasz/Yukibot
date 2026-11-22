"""Adapt the existing portal services to the standalone diagnostics API."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import Request

import config
from _async_utils import run_blocking
from _security import Power_Level
from web_api.network_models import (
    NETWORK_LIMITS,
    NetworkConfiguration,
    NetworkContext,
    NetworkIdentity,
    NetworkNodeContext,
    NetworkTarget,
)
from web_api.network_reports import NetworkReportStore

from .constants import _PORTAL_HEALTH_PATH
from .remote_node_monitor import RemoteNodeAvailability
from .service_base import ModWebServiceSupport


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
        return NetworkConfiguration(
            identity=identity, targets=targets, limits=NETWORK_LIMITS
        )

    async def network_context(self) -> NetworkContext:
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

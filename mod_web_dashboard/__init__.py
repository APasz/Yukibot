from __future__ import annotations

import importlib
from typing import cast

from . import nicegui_protocols, types

ModWebDashboardBackend: object
ModWebFastApiApp: object
ModWebNavigate: object
ModWebRefreshable: object
ModWebRouteCallable: object
ModWebRouteUi: object
ModWebRunnerUi: object
ModWebService: object
ModWebServiceSupport: object
ModWebUi: object
WebChatRelayPublisher: object


def __getattr__(name: str) -> object:
    if name == "ModWebDashboardBackend":
        backend_module = importlib.import_module(".backend", __name__)
        return cast(object, backend_module.ModWebDashboardBackend)
    if name in {
        "ModWebFastApiApp",
        "ModWebNavigate",
        "ModWebRefreshable",
        "ModWebRouteCallable",
        "ModWebRouteUi",
        "ModWebRunnerUi",
        "ModWebUi",
        "WebChatRelayPublisher",
    }:
        if name == "ModWebFastApiApp":
            return nicegui_protocols.ModWebFastApiApp
        if name == "ModWebNavigate":
            return nicegui_protocols.ModWebNavigate
        if name == "ModWebRefreshable":
            return nicegui_protocols.ModWebRefreshable
        if name == "ModWebRouteCallable":
            return nicegui_protocols.ModWebRouteCallable
        if name == "ModWebRouteUi":
            return nicegui_protocols.ModWebRouteUi
        if name == "ModWebRunnerUi":
            return nicegui_protocols.ModWebRunnerUi
        if name == "ModWebUi":
            return nicegui_protocols.ModWebUi
        return nicegui_protocols.WebChatRelayPublisher
    if name == "ModWebService":
        service_module = importlib.import_module(".service", __name__)
        return cast(object, service_module.ModWebService)
    if name == "ModWebServiceSupport":
        service_base_module = importlib.import_module(".service_base", __name__)
        return cast(object, service_base_module.ModWebServiceSupport)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)


__all__: tuple[str, ...] = (
    "ModWebDashboardBackend",
    "ModWebFastApiApp",
    "ModWebNavigate",
    "ModWebRefreshable",
    "ModWebRouteCallable",
    "ModWebRouteUi",
    "ModWebRunnerUi",
    "ModWebService",
    "ModWebServiceSupport",
    "ModWebUi",
    "WebChatRelayPublisher",
    "nicegui_protocols",
    "types",
)

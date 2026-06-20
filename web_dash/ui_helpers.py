from __future__ import annotations

from typing import TYPE_CHECKING

from font_assets import font_assets

from .runtime_imports import (
    BadgeTone,
    Callable,
    Html,
    Label,
    MOD_WEB_ACTION_BASE_CLASSES,
    Request,
    Tooltip,
    apply_mod_web_theme,
    config,
    escape,
    json,
    mod_web_badge_class,
    parse_qsl,
    cast,
    urlencode,
    urlsplit,
    urlunsplit,
)
from .constants import (
    _APP_LIST_API_QUERY_PARAM,
    _DEV_SIMULATED_DOWN_NODE_QUERY_PARAM,
    _HOME_NODE_LATENCY_REFRESH_INTERVAL_SECONDS,
    _HOME_NODE_LATENCY_TIMEOUT_SECONDS,
    _NODE_PRESENCE_RECONNECT_DELAY_SECONDS,
)
from .nicegui_protocols import ModWebUi

from .service_base import ModWebServiceSupport
from .types import _ModWebBadgeSpec, _ModWebNodePresenceBadgeSpec

if TYPE_CHECKING:
    from nicegui.element import Element

class ModWebUiHelpersMixin(ModWebServiceSupport):
    @staticmethod
    def _badge_class_name(*, tone: BadgeTone, extra_classes: str = "") -> str:
        return f"{mod_web_badge_class(tone)} {extra_classes}".strip()

    @staticmethod
    def _resolved_badge_tooltip_text(*, text: str, tooltip_text: str | None) -> str:
        return text if tooltip_text is None else tooltip_text

    @staticmethod
    def _attach_badge_tooltip(*, ui: ModWebUi, target: "Element", text: str) -> None:
        if not hasattr(ui, "tooltip"):
            return
        with target:
            try:
                ui.tooltip(text)
            except TypeError:
                return

    @staticmethod
    def _apply_theme(*, ui: ModWebUi) -> None:
        apply_mod_web_theme(ui=ui)
        font_face_css_html = font_assets.font_face_css_html(base_path="/mod-web/assets/fonts")
        if font_face_css_html:
            ui.add_head_html(font_face_css_html)

    @staticmethod
    def _badge(
        *,
        ui: ModWebUi,
        text: str,
        tone: BadgeTone,
        extra_classes: str = "",
        tooltip_text: str | None = None,
    ) -> Label:
        badge = ui.label(text).classes(ModWebUiHelpersMixin._badge_class_name(tone=tone, extra_classes=extra_classes))
        ModWebUiHelpersMixin._attach_badge_tooltip(
            ui=ui,
            target=cast("Element", badge),
            text=ModWebUiHelpersMixin._resolved_badge_tooltip_text(text=text, tooltip_text=tooltip_text),
        )
        return badge

    @staticmethod
    def _badge_clickable(
        *,
        ui: ModWebUi,
        text: str,
        tone: BadgeTone,
        on_click: Callable[[object | None], object],
        extra_classes: str = "",
        tooltip_text: str | None = None,
    ) -> Label:
        badge = ui.label(text).classes(ModWebUiHelpersMixin._badge_class_name(tone=tone, extra_classes=extra_classes)).props(
            "role=button tabindex=0"
        ).on("click", on_click)
        ModWebUiHelpersMixin._attach_badge_tooltip(
            ui=ui,
            target=cast("Element", badge),
            text=ModWebUiHelpersMixin._resolved_badge_tooltip_text(text=text, tooltip_text=tooltip_text),
        )
        return badge

    @staticmethod
    def _badge_icon(
        *,
        ui: ModWebUi,
        text: str,
        tone: BadgeTone,
        icon: str,
        extra_classes: str = "",
        tooltip_text: str | None = None,
    ) -> "Element":
        badge = ui.element("span").classes(
            ModWebUiHelpersMixin._badge_class_name(tone=tone, extra_classes=f"mod-badge-icon-label {extra_classes}".strip())
        )
        with badge:
            ui.label(text).classes("mod-badge-value")
            ui.icon(icon).classes("mod-badge-icon")
        ModWebUiHelpersMixin._attach_badge_tooltip(
            ui=ui,
            target=badge,
            text=ModWebUiHelpersMixin._resolved_badge_tooltip_text(text=text, tooltip_text=tooltip_text),
        )
        return badge

    def _badge_spec(self, *, ui: ModWebUi, badge: _ModWebBadgeSpec, extra_classes: str = "") -> "Element":
        if badge.icon is None:
            return cast(
                "Element",
                self._badge(
                    ui=ui,
                    text=badge.text,
                    tone=badge.tone,
                    extra_classes=extra_classes,
                    tooltip_text=badge.tooltip_text,
                ),
            )
        return self._badge_icon(
            ui=ui,
            text=badge.text,
            tone=badge.tone,
            icon=badge.icon,
            extra_classes=extra_classes,
            tooltip_text=badge.tooltip_text,
        )

    def _interactive_badge(
        self,
        *,
        ui: ModWebUi,
        text: str,
        tone: BadgeTone,
        url: str | None = None,
        on_click: Callable[[object | None], object] | None = None,
        tooltip_text: str | None = None,
        extra_classes: str = "",
    ) -> "Element":
        if url is None and on_click is not None:
            badge = cast(
                "Element",
                self._badge_clickable(
                    ui=ui,
                    text=text,
                    tone=tone,
                    on_click=on_click,
                    extra_classes=extra_classes,
                    tooltip_text=tooltip_text,
                ),
            )
        elif url is None:
            badge = cast(
                "Element",
                self._badge(ui=ui, text=text, tone=tone, extra_classes=extra_classes, tooltip_text=tooltip_text),
            )
        else:
            badge = self._badge_link(
                ui=ui,
                text=text,
                tone=tone,
                url=url,
                extra_classes=extra_classes,
                tooltip_text=tooltip_text,
            )
        return badge

    @staticmethod
    def _badge_link(
        *,
        ui: ModWebUi,
        text: str,
        tone: BadgeTone,
        url: str,
        new_tab: bool = False,
        shift_url: str | None = None,
        stop_propagation: bool = False,
        extra_classes: str = "",
        tooltip_text: str | None = None,
    ) -> "Element":
        badge = ui.link(text, url).classes(
            ModWebUiHelpersMixin._badge_class_name(
                tone=tone,
                extra_classes=f"mod-badge-link cursor-pointer {extra_classes}".strip(),
            )
        )
        ModWebUiHelpersMixin._attach_badge_tooltip(
            ui=ui,
            target=badge,
            text=ModWebUiHelpersMixin._resolved_badge_tooltip_text(text=text, tooltip_text=tooltip_text),
        )
        if new_tab:
            badge.props('target="_blank" rel="noopener noreferrer"')
        if shift_url is not None:
            encoded_shift_url: str = json.dumps(shift_url)
            stop_propagation_js: str = "event.stopPropagation();" if stop_propagation else ""
            badge.on(
                "click",
                js_handler=(
                    "(event) => {"
                    "if (event.shiftKey) {"
                    "event.preventDefault();"
                    f"{stop_propagation_js}"
                    f"const opened = window.open({encoded_shift_url}, '_blank', 'noopener,noreferrer');"
                    "if (opened) { opened.opener = null; }"
                    "return;"
                    "}"
                    f"{stop_propagation_js}"
                    "}"
                ),
            )
        elif stop_propagation:
            badge.on("click", js_handler="(event) => event.stopPropagation()")
        return badge

    @staticmethod
    def _action_link(
        *,
        ui: ModWebUi,
        label: str,
        url: str,
        compact: bool = False,
        extra_classes: str = "",
        stop_propagation: bool = False,
        new_tab: bool = False,
    ) -> None:
        padding = "px-4 py-2 text-sm" if compact else "px-5 py-3 text-base"
        link = ui.link(label, url).classes(f"{MOD_WEB_ACTION_BASE_CLASSES} {padding} {extra_classes}".strip())
        if new_tab:
            link.props('target="_blank" rel="noopener noreferrer"')
        if stop_propagation:
            link.on("click", js_handler="(event) => event.stopPropagation()")

    @classmethod
    def _run_node_presence_badges_javascript(
        cls,
        *,
        ui: ModWebUi,
        badge_specs: tuple[_ModWebNodePresenceBadgeSpec, ...],
        controller_key: str,
    ) -> None:
        if not badge_specs:
            return
        ui.run_javascript(cls._node_presence_badges_javascript(badge_specs=badge_specs, controller_key=controller_key), timeout=0.1)

    @classmethod
    def _node_presence_badges_javascript(
        cls,
        *,
        badge_specs: tuple[_ModWebNodePresenceBadgeSpec, ...],
        controller_key: str,
    ) -> str:
        specs_json: str = json.dumps([badge_spec.to_mapping() for badge_spec in badge_specs], separators=(",", ":"))
        latency_refresh_interval_ms: int = int(_HOME_NODE_LATENCY_REFRESH_INTERVAL_SECONDS * 1000)
        latency_timeout_ms: int = int(_HOME_NODE_LATENCY_TIMEOUT_SECONDS * 1000)
        reconnect_delay_ms: int = int(_NODE_PRESENCE_RECONNECT_DELAY_SECONDS * 1000)
        return f"""
            (() => {{
                const controllerKey = {json.dumps(controller_key)};
                const specs = {specs_json};
                const latencyRefreshIntervalMs = {latency_refresh_interval_ms};
                const latencyTimeoutMs = {latency_timeout_ms};
                const reconnectDelayMs = {reconnect_delay_ms};
                const bootstrapProbeCount = 4;
                const bootstrapProbeDelayMs = 850;
                const getElementMaybe = (elementId) => getElement(elementId) || getHtmlElement(elementId);
                const websocketUrl = (presenceStreamUrl) => {{
                    const resolved = new URL(presenceStreamUrl, window.location.href);
                    resolved.protocol = resolved.protocol === 'https:' ? 'wss:' : 'ws:';
                    return resolved.toString();
                }};
                const existing = window[controllerKey];
                const controllerState = existing && typeof existing === 'object'
                    ? existing
                    : {{connectionsByNode: {{}}, specByNode: {{}}}};
                controllerState.connectionsByNode = controllerState.connectionsByNode || {{}};
                controllerState.specByNode = Object.fromEntries(specs.map((spec) => [spec.node_name, spec]));
                const getSpec = (nodeName) => controllerState.specByNode[nodeName] || null;
                const renderBadge = (spec, text, className) => {{
                    const badgeElement = getElementMaybe(spec.badge_element_id);
                    if (badgeElement) {{
                        badgeElement.className = className;
                    }}
                    const textElement = spec.text_element_id == null ? badgeElement : getElementMaybe(spec.text_element_id);
                    if (textElement) {{
                        textElement.textContent = text;
                    }}
                }};
                const latencyText = (spec, latencyTextValue) => `${{spec.node_label}}: ${{latencyTextValue}}`;
                const formatLatency = (latencyMs) => {{
                    if (typeof latencyMs !== 'number' || !Number.isFinite(latencyMs)) {{
                        return null;
                    }}
                    return `${{latencyMs}} ms`;
                }};
                const sleep = async (delayMs) => new Promise((resolve) => window.setTimeout(resolve, delayMs));
                const summariseLatencyMeasurements = (measurements) => {{
                    if (!measurements.length) {{
                        return null;
                    }}
                    const sorted = [...measurements].sort((left, right) => left - right);
                    const middle = Math.floor(sorted.length / 2);
                    if (sorted.length % 2 === 1) {{
                        return sorted[middle];
                    }}
                    return Math.round((sorted[middle - 1] + sorted[middle]) / 2);
                }};
                const rejectPendingSamples = (connection) => {{
                    for (const [sampleId, pending] of Object.entries(connection.pendingSamples || {{}})) {{
                        window.clearTimeout(pending.timeoutHandle);
                        pending.resolve(null);
                        delete connection.pendingSamples[sampleId];
                    }}
                }};
                const closeConnection = (nodeName) => {{
                    const connection = controllerState.connectionsByNode[nodeName];
                    if (!connection) {{
                        return;
                    }}
                    connection.closedByScript = true;
                    if (connection.reconnectTimeoutId) {{
                        window.clearTimeout(connection.reconnectTimeoutId);
                    }}
                    if (connection.latencyIntervalId) {{
                        window.clearInterval(connection.latencyIntervalId);
                    }}
                    rejectPendingSamples(connection);
                    if (connection.socket) {{
                        connection.socket.close();
                    }}
                    delete controllerState.connectionsByNode[nodeName];
                }};
                const sampleLatencyMeasurement = async (nodeName) => {{
                    const connection = controllerState.connectionsByNode[nodeName];
                    const spec = getSpec(nodeName);
                    if (!connection || !spec || !spec.show_latency || !connection.socket || connection.socket.readyState !== WebSocket.OPEN) {{
                        return null;
                    }}
                    return await new Promise((resolve) => {{
                        const sampleId = `${{Date.now()}}-${{Math.random().toString(16).slice(2)}}`;
                        const startedAt = performance.now();
                        const timeoutHandle = window.setTimeout(() => {{
                            delete connection.pendingSamples[sampleId];
                            resolve(null);
                        }}, latencyTimeoutMs);
                        connection.pendingSamples[sampleId] = {{
                            timeoutHandle,
                            resolve: () => {{
                                window.clearTimeout(timeoutHandle);
                                delete connection.pendingSamples[sampleId];
                                resolve(Math.max(1, Math.round(performance.now() - startedAt)));
                            }},
                        }};
                        connection.socket.send(JSON.stringify({{type: 'ping', sample_id: sampleId}}));
                    }});
                }};
                const renderAliveState = async (nodeName, latencyTextValue) => {{
                    const connection = controllerState.connectionsByNode[nodeName];
                    const spec = getSpec(nodeName);
                    if (!connection || !spec) {{
                        return;
                    }}
                    const nextText = latencyTextValue === null ? spec.alive_text : latencyText(spec, latencyTextValue);
                    connection.lastText = nextText;
                    connection.lastClassName = spec.healthy_class_name;
                    renderBadge(spec, nextText, spec.healthy_class_name);
                }};
                const runLatencyBootstrap = async (nodeName) => {{
                    const spec = getSpec(nodeName);
                    if (!spec || !spec.show_latency) {{
                        return;
                    }}
                    const measurements = [];
                    for (let attemptIndex = 0; attemptIndex < bootstrapProbeCount; attemptIndex += 1) {{
                        const latency = await sampleLatencyMeasurement(nodeName);
                        if (typeof latency === 'number' && Number.isFinite(latency)) {{
                            measurements.push(latency);
                        }}
                        if (attemptIndex + 1 < bootstrapProbeCount) {{
                            await sleep(bootstrapProbeDelayMs);
                        }}
                    }}
                    const summary = summariseLatencyMeasurements(measurements);
                    await renderAliveState(nodeName, formatLatency(summary));
                }};
                const runLatencySample = async (nodeName) => {{
                    const spec = getSpec(nodeName);
                    if (!spec || !spec.show_latency) {{
                        return;
                    }}
                    const latency = await sampleLatencyMeasurement(nodeName);
                    await renderAliveState(nodeName, formatLatency(latency));
                }};
                const scheduleReconnect = (nodeName) => {{
                    const connection = controllerState.connectionsByNode[nodeName];
                    if (!connection || connection.closedByScript) {{
                        return;
                    }}
                    if (connection.reconnectTimeoutId) {{
                        window.clearTimeout(connection.reconnectTimeoutId);
                    }}
                    connection.reconnectTimeoutId = window.setTimeout(() => {{
                        void connect(nodeName);
                    }}, reconnectDelayMs);
                }};
                const connect = async (nodeName) => {{
                    const spec = getSpec(nodeName);
                    if (!spec) {{
                        closeConnection(nodeName);
                        return;
                    }}
                    if (!spec.presence_stream_url) {{
                        renderBadge(spec, spec.pending_text, spec.pending_class_name);
                        closeConnection(nodeName);
                        return;
                    }}
                    const existingConnection = controllerState.connectionsByNode[nodeName];
                    if (existingConnection && existingConnection.socket) {{
                        if (existingConnection.socket.readyState === WebSocket.OPEN || existingConnection.socket.readyState === WebSocket.CONNECTING) {{
                            renderBadge(spec, existingConnection.lastText || spec.pending_text, existingConnection.lastClassName || spec.pending_class_name);
                            return;
                        }}
                        closeConnection(nodeName);
                    }}
                    const connection = {{
                        socket: null,
                        reconnectTimeoutId: null,
                        latencyIntervalId: null,
                        pendingSamples: {{}},
                        closedByScript: false,
                        lastText: spec.pending_text,
                        lastClassName: spec.pending_class_name,
                    }};
                    controllerState.connectionsByNode[nodeName] = connection;
                    renderBadge(spec, spec.pending_text, spec.pending_class_name);
                    const socket = new WebSocket(websocketUrl(spec.presence_stream_url));
                    connection.socket = socket;
                    socket.addEventListener('open', () => {{
                        const latestSpec = getSpec(nodeName);
                        if (!latestSpec) {{
                            closeConnection(nodeName);
                            return;
                        }}
                        connection.lastText = latestSpec.show_latency ? latestSpec.pending_text : latestSpec.alive_text;
                        connection.lastClassName = latestSpec.healthy_class_name;
                        renderBadge(latestSpec, connection.lastText, connection.lastClassName);
                        rejectPendingSamples(connection);
                        if (connection.latencyIntervalId) {{
                            window.clearInterval(connection.latencyIntervalId);
                        }}
                        if (latestSpec.show_latency) {{
                            void runLatencyBootstrap(nodeName);
                            connection.latencyIntervalId = window.setInterval(() => {{
                                void runLatencySample(nodeName);
                            }}, latencyRefreshIntervalMs);
                        }}
                    }});
                    socket.addEventListener('message', (event) => {{
                        let payload = null;
                        try {{
                            payload = JSON.parse(event.data);
                        }} catch (_error) {{
                            return;
                        }}
                        if (!payload || typeof payload !== 'object') {{
                            return;
                        }}
                        const sampleId = payload.sample_id;
                        if (sampleId == null) {{
                            return;
                        }}
                        const pending = connection.pendingSamples[String(sampleId)];
                        if (!pending) {{
                            return;
                        }}
                        pending.resolve();
                    }});
                    const markDisconnected = () => {{
                        const latestConnection = controllerState.connectionsByNode[nodeName];
                        const latestSpec = getSpec(nodeName);
                        if (!latestConnection || latestConnection !== connection || !latestSpec) {{
                            return;
                        }}
                        if (connection.latencyIntervalId) {{
                            window.clearInterval(connection.latencyIntervalId);
                            connection.latencyIntervalId = null;
                        }}
                        rejectPendingSamples(connection);
                        connection.lastText = latestSpec.down_text;
                        connection.lastClassName = latestSpec.unhealthy_class_name;
                        renderBadge(latestSpec, latestSpec.down_text, latestSpec.unhealthy_class_name);
                        scheduleReconnect(nodeName);
                    }};
                    socket.addEventListener('close', markDisconnected);
                    socket.addEventListener('error', () => {{
                        if (socket.readyState === WebSocket.CLOSED) {{
                            markDisconnected();
                        }}
                    }});
                }};
                const activeNodeNames = new Set(specs.map((spec) => spec.node_name));
                for (const nodeName of Object.keys(controllerState.connectionsByNode)) {{
                    if (!activeNodeNames.has(nodeName)) {{
                        closeConnection(nodeName);
                    }}
                }}
                for (const spec of specs) {{
                    void connect(spec.node_name);
                }}
                window[controllerKey] = controllerState;
            }})();
        """

    @staticmethod
    def _attach_html_tooltip(*, ui: ModWebUi, target: "Element", html: str = "") -> tuple[Tooltip, Html]:
        with target:
            with ui.tooltip() as tooltip:
                tooltip_content = cast(Html, ui.html(html))
        return tooltip, tooltip_content

    @staticmethod
    def _set_html_tooltip_state(tooltip: Tooltip, tooltip_content: Html, html: str) -> None:
        tooltip_content.set_content(html)
        tooltip_content.update()
        tooltip.update()

    @staticmethod
    def _tooltip_lines_html(lines: tuple[str, ...]) -> str | None:
        tooltip_lines: tuple[str, ...] = tuple[str, ...](line for line in lines if line.strip())
        if not tooltip_lines:
            return None
        return "<br>".join(escape(line) for line in tooltip_lines)

    def _player_count_tooltip_html(
        self,
        *,
        player_count: int | None,
        player_capacity: int | None,
        connected_player_names: tuple[str, ...],
    ) -> str | None:
        if player_count is None or player_capacity is None:
            return None
        return self._tooltip_lines_html(connected_player_names)

    @staticmethod
    def _app_list_api_actions_enabled(request: Request) -> bool:
        value = request.query_params.get(_APP_LIST_API_QUERY_PARAM)
        if value is None:
            return False
        return value.strip().casefold() in {"1", "true", "yes", "on"}

    def _simulated_down_node_names(self, request: Request) -> tuple[str, ...]:
        if not config.INDEV:
            return ()
        requested_keys = {
            raw_name.strip().casefold()
            for raw_name in request.query_params.getlist(_DEV_SIMULATED_DOWN_NODE_QUERY_PARAM)
            if raw_name.strip()
        }
        if not requested_keys:
            return ()
        return tuple(
            node.node_name
            for node in self._node_links()
            if node.node_name.strip().casefold() in requested_keys
        )

    def _toggle_simulated_down_node_url(
        self,
        *,
        current_url: str,
        node_name: str,
        simulated_down_node_names: tuple[str, ...],
    ) -> str:
        known_node_names = tuple(node.node_name for node in self._node_links())
        known_node_name_keys = {known_name.casefold() for known_name in known_node_names}
        node_key = node_name.strip().casefold()
        if node_key not in known_node_name_keys:
            raise ValueError(f"Cannot simulate unknown node as down: {node_name!r}")

        updated_keys = {configured_name.casefold() for configured_name in simulated_down_node_names}
        if node_key in updated_keys:
            updated_keys.remove(node_key)
        else:
            updated_keys.add(node_key)

        updated_node_names = tuple(
            known_name for known_name in known_node_names if known_name.casefold() in updated_keys
        )
        return self._request_url_with_query_values(
            current_url,
            param_name=_DEV_SIMULATED_DOWN_NODE_QUERY_PARAM,
            values=updated_node_names,
        )

    @staticmethod
    def _app_list_view_url(url: str, *, show_api_actions: bool) -> str:
        parts = urlsplit(url)
        query_items = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)]
        query_by_key: dict[str, str] = {key: value for key, value in query_items if key != _APP_LIST_API_QUERY_PARAM}
        if show_api_actions:
            query_by_key[_APP_LIST_API_QUERY_PARAM] = "1"
        query = urlencode(query_by_key)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))

    @staticmethod
    def _request_url_with_query_values(url: str, *, param_name: str, values: tuple[str, ...]) -> str:
        parts = urlsplit(url)
        query_items = [
            (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != param_name
        ]
        query_items.extend((param_name, value) for value in values)
        query = urlencode(query_items)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))

    @staticmethod
    def _request_path(request: Request) -> str:
        url = getattr(request, "url", None)
        path = getattr(url, "path", None)
        if isinstance(path, str) and path.startswith("/") and not path.startswith("//"):
            query = getattr(url, "query", None)
            if isinstance(query, str) and query:
                return f"{path}?{query}"
            return path
        return "/"

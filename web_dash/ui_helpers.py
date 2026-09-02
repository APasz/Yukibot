from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal

from font_assets import font_assets
from node_api.route_contracts import (
    NODE_DISCORD_HEARTBEAT_LATENCY_HEADER,
    NODE_DISCORD_SERVICE_STATE_HEADER,
)

from .constants import (
    _APP_LIST_API_QUERY_PARAM,
    _HOME_NODE_LATENCY_REFRESH_INTERVAL_SECONDS,
    _HOME_NODE_LATENCY_TIMEOUT_SECONDS,
    _NODE_PRESENCE_RECONNECT_DELAY_SECONDS,
    _PORTAL_HEALTH_PATH,
    _PORTAL_RECOVERY_HEALTH_TIMEOUT_SECONDS,
    _PORTAL_RECOVERY_IDLE_THRESHOLD_SECONDS,
    _PORTAL_RECOVERY_POLL_INTERVAL_SECONDS,
)
from .nicegui_protocols import ModWebUi
from .runtime_imports import (
    MOD_WEB_ACTION_BASE_CLASSES,
    BadgeTone,
    Callable,
    Html,
    Label,
    Request,
    Tooltip,
    apply_mod_web_theme,
    cast,
    escape,
    json,
    mod_web_badge_class,
    parse_qsl,
    urlencode,
    urlsplit,
    urlunsplit,
)
from .service_base import ModWebServiceSupport
from .types import _ModWebBadgeSpec, _ModWebNodePresenceBadgeSpec

if TYPE_CHECKING:
    from nicegui.element import Element

_LIVE_VALUE_PULSE_CLASSES: tuple[str, str] = (
    "mod-live-value-pulse-a",
    "mod-live-value-pulse-b",
)
_MAIN_CONTENT_ID: Final[str] = "mod-main-content"
_POINTER_NAVIGATION_CLASS: Final[str] = "mod-pointer-navigation"

def copy_text_to_clipboard(
    *,
    ui: ModWebUi,
    text: str,
    empty_message: str,
) -> bool:
    normalised_text: str = text.strip()
    if not normalised_text:
        ui.notify(empty_message, type="warning")
        return False
    encoded_text: str = json.dumps(normalised_text)
    ui.run_javascript(
        (
            "(async () => {"
            f"const text = {encoded_text};"
            "try {"
            "if (navigator.clipboard && navigator.clipboard.writeText) {"
            "await navigator.clipboard.writeText(text);"
            "} else {"
            "const textarea = document.createElement('textarea');"
            "textarea.value = text;"
            "textarea.setAttribute('readonly', 'true');"
            "textarea.style.position = 'fixed';"
            "textarea.style.opacity = '0';"
            "document.body.appendChild(textarea);"
            "textarea.focus();"
            "textarea.select();"
            "document.execCommand('copy');"
            "document.body.removeChild(textarea);"
            "}"
            "} catch (_error) {}"
            "})()"
        ),
        timeout=1.0,
    )
    return True


class ModWebUiHelpersMixin(ModWebServiceSupport):
    @staticmethod
    def _render_skip_link(*, ui: ModWebUi, target_id: str = _MAIN_CONTENT_ID) -> None:
        with ui.element("a").props(f"href=#{target_id}").classes("mod-skip-link"):
            ui.label("Skip to main content")

    @staticmethod
    def _make_activatable(
        *,
        target: "Element",
        role: Literal["button", "link"],
        on_activate: Callable[[object | None], object],
    ) -> None:
        target.props(f"role={role} tabindex=0")
        target.on("click", on_activate)
        target.on(
            "keydown.enter",
            on_activate,
            js_handler="(event) => { event.preventDefault(); emit(); }",
        )
        target.on(
            "keydown.space",
            on_activate,
            js_handler="(event) => { event.preventDefault(); emit(); }",
        )

    @staticmethod
    def _portal_recovery_head_html() -> str:
        health_path: str = json.dumps(_PORTAL_HEALTH_PATH)
        health_timeout_ms: int = round(_PORTAL_RECOVERY_HEALTH_TIMEOUT_SECONDS * 1000)
        idle_threshold_ms: int = round(_PORTAL_RECOVERY_IDLE_THRESHOLD_SECONDS * 1000)
        poll_interval_ms: int = round(_PORTAL_RECOVERY_POLL_INTERVAL_SECONDS * 1000)
        return f"""
            <script>
            (() => {{
              const existing = window.modWebPortalRecovery;
              if (existing?.version === 1) {{
                return;
              }}
              const healthPath = {health_path};
              const healthTimeoutMs = {health_timeout_ms};
              const idleThresholdMs = {idle_threshold_ms};
              const pollIntervalMs = {poll_interval_ms};
              const overlayId = 'mod-web-recovery-overlay';
              const styleId = 'mod-web-recovery-style';
              let lastTick = Date.now();
              let hiddenAt = document.visibilityState === 'hidden' ? Date.now() : null;
              let recoveryActive = false;
              let reloadScheduled = false;

              const installStyle = () => {{
                if (document.getElementById(styleId)) {{
                  return;
                }}
                const style = document.createElement('style');
                style.id = styleId;
                style.textContent = `
                  #${{overlayId}} {{
                    position: fixed;
                    inset: 0;
                    z-index: 2147483000;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    padding: 24px;
                    background: rgba(8, 8, 12, 0.72);
                    backdrop-filter: blur(8px);
                    color: #fafafa;
                    font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                  }}
                  #${{overlayId}} .mod-web-recovery-panel {{
                    width: min(100%, 420px);
                    border: 1px solid rgba(245, 158, 11, 0.55);
                    background: rgba(18, 18, 24, 0.96);
                    box-shadow: 0 24px 60px rgba(0, 0, 0, 0.46);
                    padding: 20px;
                  }}
                  #${{overlayId}} .mod-web-recovery-title {{
                    margin: 0 0 8px;
                    color: #fbbf24;
                    font-size: 13px;
                    font-weight: 700;
                    letter-spacing: 0;
                    text-transform: uppercase;
                  }}
                  #${{overlayId}} .mod-web-recovery-message {{
                    margin: 0 0 16px;
                    color: #f4f4f5;
                    font-size: 14px;
                    line-height: 1.5;
                  }}
                  #${{overlayId}} .mod-web-recovery-actions {{
                    display: flex;
                    flex-wrap: wrap;
                    gap: 8px;
                  }}
                  #${{overlayId}} button {{
                    border: 1px solid rgba(250, 250, 250, 0.16);
                    background: rgba(250, 250, 250, 0.08);
                    color: #fafafa;
                    cursor: pointer;
                    font: inherit;
                    font-size: 13px;
                    min-height: 34px;
                    padding: 7px 11px;
                  }}
                  #${{overlayId}} button.primary {{
                    border-color: rgba(245, 158, 11, 0.72);
                    background: rgba(245, 158, 11, 0.18);
                    color: #fde68a;
                  }}
                `;
                document.head.appendChild(style);
              }};

              const ensureOverlay = message => {{
                installStyle();
                let overlay = document.getElementById(overlayId);
                if (!overlay) {{
                  overlay = document.createElement('div');
                  overlay.id = overlayId;
                  overlay.innerHTML = `
                    <div class="mod-web-recovery-panel" role="status" aria-live="polite">
                      <p class="mod-web-recovery-title">Connection interrupted</p>
                      <p class="mod-web-recovery-message"></p>
                      <div class="mod-web-recovery-actions">
                        <button class="primary" type="button" data-mod-web-recovery-reload>Reload portal</button>
                        <button type="button" data-mod-web-recovery-home>Home</button>
                      </div>
                    </div>
                  `;
                  overlay.querySelector('[data-mod-web-recovery-reload]')?.addEventListener('click', () => hardReload());
                  overlay.querySelector('[data-mod-web-recovery-home]')?.addEventListener('click', () => {{
                    window.location.assign('/');
                  }});
                  document.body.appendChild(overlay);
                }}
                const messageElement = overlay.querySelector('.mod-web-recovery-message');
                if (messageElement) {{
                  messageElement.textContent = message;
                }}
              }};

              const hideOverlay = () => {{
                document.getElementById(overlayId)?.remove();
              }};

              const probeHealth = async () => {{
                const controller = new AbortController();
                const timeoutId = window.setTimeout(() => controller.abort(), healthTimeoutMs);
                try {{
                  const response = await fetch(healthPath, {{
                    cache: 'no-store',
                    credentials: 'same-origin',
                    headers: {{ Accept: 'application/json' }},
                    signal: controller.signal,
                  }});
                  return response.ok;
                }} finally {{
                  window.clearTimeout(timeoutId);
                }}
              }};

              const hardReload = reason => {{
                if (reloadScheduled) {{
                  return;
                }}
                reloadScheduled = true;
                ensureOverlay(reason || 'Refreshing the portal...');
                window.setTimeout(() => window.location.reload(), 250);
              }};

              const recover = async (reason, shouldReload) => {{
                if (recoveryActive || reloadScheduled) {{
                  return;
                }}
                recoveryActive = true;
                ensureOverlay(reason || 'Reconnecting to the portal...');
                try {{
                  if (await probeHealth()) {{
                    if (shouldReload) {{
                      hardReload('Refreshing the portal connection...');
                    }} else {{
                      hideOverlay();
                    }}
                    return;
                  }}
                  ensureOverlay('The portal is not responding yet. Retrying shortly...');
                  window.setTimeout(() => {{
                    recoveryActive = false;
                    recover('Retrying portal connection...', shouldReload);
                  }}, 2000);
                }} catch (_error) {{
                  ensureOverlay(navigator.onLine ? 'The portal is not responding yet. Retrying shortly...' : 'The browser is offline. Waiting for the network...');
                  window.setTimeout(() => {{
                    recoveryActive = false;
                    recover('Retrying portal connection...', shouldReload);
                  }}, 2000);
                }} finally {{
                  if (!reloadScheduled) {{
                    recoveryActive = false;
                  }}
                }}
              }};

              window.setInterval(() => {{
                const now = Date.now();
                const drift = now - lastTick - pollIntervalMs;
                lastTick = now;
                if (document.visibilityState === 'visible' && drift > idleThresholdMs) {{
                  recover('The portal was idle for a while. Reconnecting...', true);
                }}
              }}, pollIntervalMs);

              document.addEventListener('visibilitychange', () => {{
                if (document.visibilityState === 'hidden') {{
                  hiddenAt = Date.now();
                  return;
                }}
                const hiddenDuration = hiddenAt === null ? 0 : Date.now() - hiddenAt;
                hiddenAt = null;
                lastTick = Date.now();
                if (hiddenDuration > idleThresholdMs) {{
                  recover('The portal was idle for a while. Reconnecting...', true);
                }}
              }});

              window.addEventListener('pageshow', event => {{
                if (event.persisted) {{
                  recover('Restoring the portal connection...', true);
                }}
              }});
              window.addEventListener('online', () => recover('Network restored. Reconnecting...', true));

              window.setTimeout(() => {{
                const bodyText = document.body?.innerText?.trim() || '';
                const hasPortalContent = Boolean(document.querySelector('.mod-page, .q-layout, .nicegui-content'));
                if (!bodyText && !hasPortalContent) {{
                  ensureOverlay('The portal did not finish loading. Reload to try again.');
                }}
              }}, 8000);

              window.modWebPortalRecovery = {{
                version: 1,
                reload: hardReload,
                recover,
              }};
            }})();
            </script>
        """

    @staticmethod
    def _focus_modality_head_html() -> str:
        return f"""
            <script>
            (() => {{
              const root = document.documentElement;
              if (root.dataset.modWebFocusModality === 'installed') {{
                return;
              }}
              root.dataset.modWebFocusModality = 'installed';
              window.addEventListener('pointerdown', () => root.classList.add('{_POINTER_NAVIGATION_CLASS}'), true);
              window.addEventListener('keydown', () => root.classList.remove('{_POINTER_NAVIGATION_CLASS}'), true);
            }})();
            </script>
        """

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
        ui.add_head_html(ModWebUiHelpersMixin._portal_recovery_head_html())
        ui.add_head_html(ModWebUiHelpersMixin._focus_modality_head_html())
        font_face_css_html = font_assets.font_face_css_html(base_path="/mod-web/assets/fonts")
        if font_face_css_html:
            ui.add_head_html(font_face_css_html)

    @staticmethod
    def _guarded_reload(*, ui: ModWebUi, reason: str = "Refreshing the portal...") -> None:
        encoded_reason: str = json.dumps(reason)
        try:
            ui.run_javascript(
                (
                    "(() => {"
                    f"const reason = {encoded_reason};"
                    "if (window.modWebPortalRecovery?.reload) {"
                    "window.modWebPortalRecovery.reload(reason);"
                    "} else {"
                    "window.location.reload();"
                    "}"
                    "})()"
                ),
                timeout=0.1,
            )
        except Exception:
            ui.navigate.reload()

    @staticmethod
    def _pulse_live_value(element: "Element") -> None:
        current_variant: int = int(getattr(element, "_mod_live_value_pulse_variant", 0))
        next_variant: int = (current_variant + 1) % len(_LIVE_VALUE_PULSE_CLASSES)
        setattr(element, "_mod_live_value_pulse_variant", next_variant)
        element.classes(remove=" ".join(_LIVE_VALUE_PULSE_CLASSES))
        element.classes(add=_LIVE_VALUE_PULSE_CLASSES[next_variant])

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
        badge = ui.label(text).classes(
            ModWebUiHelpersMixin._badge_class_name(
                tone=tone,
                extra_classes=f"mod-badge-action {extra_classes}".strip(),
            )
        )
        ModWebUiHelpersMixin._make_activatable(
            target=cast("Element", badge),
            role="button",
            on_activate=on_click,
        )
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
        badge, _value_label = ModWebUiHelpersMixin._badge_icon_parts(
            ui=ui,
            text=text,
            tone=tone,
            icon=icon,
            extra_classes=extra_classes,
            tooltip_text=tooltip_text,
        )
        return badge

    @staticmethod
    def _badge_icon_parts(
        *,
        ui: ModWebUi,
        text: str,
        tone: BadgeTone,
        icon: str,
        extra_classes: str = "",
        tooltip_text: str | None = None,
    ) -> tuple["Element", Label]:
        badge = ui.element("span").classes(
            ModWebUiHelpersMixin._badge_class_name(tone=tone, extra_classes=f"mod-badge-icon-label {extra_classes}".strip())
        )
        with badge:
            value_label = ui.label(text).classes("mod-badge-value")
            ui.icon(icon).classes("mod-badge-icon")
        ModWebUiHelpersMixin._attach_badge_tooltip(
            ui=ui,
            target=badge,
            text=ModWebUiHelpersMixin._resolved_badge_tooltip_text(text=text, tooltip_text=tooltip_text),
        )
        return badge, value_label

    @staticmethod
    def _badge_avatar_markup(*, avatar_uri: str, display_name: str) -> str:
        safe_avatar_uri = escape(avatar_uri, quote=True)
        safe_alt_text = escape(f"{display_name} avatar", quote=True)
        return (
            f'<img src="{safe_avatar_uri}" alt="{safe_alt_text}" '
            'loading="lazy" referrerpolicy="no-referrer">'
        )

    @staticmethod
    def _badge_avatar(
        *,
        ui: ModWebUi,
        text: str,
        tone: BadgeTone,
        avatar_uri: str,
        extra_classes: str = "",
        tooltip_text: str | None = None,
    ) -> "Element":
        badge = ui.element("span").classes(
            ModWebUiHelpersMixin._badge_class_name(
                tone=tone,
                extra_classes=f"mod-badge-avatar {extra_classes}".strip(),
            )
        )
        with badge:
            ui.html(
                ModWebUiHelpersMixin._badge_avatar_markup(
                    avatar_uri=avatar_uri,
                    display_name=text,
                )
            ).classes("mod-badge-avatar-media")
            ui.label(text).classes("mod-badge-avatar-value")
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
        discord_latency_header: str = json.dumps(NODE_DISCORD_HEARTBEAT_LATENCY_HEADER)
        discord_service_state_header: str = json.dumps(NODE_DISCORD_SERVICE_STATE_HEADER)
        return f"""
            (() => {{
                const controllerKey = {json.dumps(controller_key)};
                const specs = {specs_json};
                const latencyRefreshIntervalMs = {latency_refresh_interval_ms};
                const latencyTimeoutMs = {latency_timeout_ms};
                const reconnectDelayMs = {reconnect_delay_ms};
                const discordLatencyHeader = {discord_latency_header};
                const discordServiceStateHeader = {discord_service_state_header};
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
                const setBadgeTooltip = (spec, text) => {{
                    const tooltipState = spec.tooltip_element_id === null
                        ? null
                        : mounted_app?.elements?.[spec.tooltip_element_id];
                    if (tooltipState) {{
                        tooltipState.text = text;
                        return;
                    }}
                    const badgeElement = getElementMaybe(spec.badge_element_id);
                    if (badgeElement) {{
                        badgeElement.setAttribute('title', text);
                    }}
                }};
                const latencyText = (spec, latencyTextValue) => `${{spec.node_label}}: ${{latencyTextValue}}`;
                const formatLatency = (latencyMs) => {{
                    if (typeof latencyMs !== 'number' || !Number.isFinite(latencyMs)) {{
                        return null;
                    }}
                    return `${{latencyMs}} ms`;
                }};
                const formatTooltipLatency = (latencyMs) => formatLatency(latencyMs) || 'unavailable';
                const discordServiceText = (state, latencyMs) => {{
                    if (state === 'ready') {{
                        return latencyMs === null ? 'unavailable (commands synced)' : formatTooltipLatency(latencyMs);
                    }}
                    if (state === 'commands_ready') {{
                        return 'unavailable (gateway starting)';
                    }}
                    if (state === 'starting') {{
                        return 'command sync starting';
                    }}
                    if (state === 'degraded') {{
                        return 'Discord API degraded — retrying';
                    }}
                    if (state === 'gateway_degraded') {{
                        return 'gateway unavailable — retrying';
                    }}
                    if (state === 'failed') {{
                        return 'startup failed — operator action required';
                    }}
                    return formatTooltipLatency(latencyMs);
                }};
                const tooltipText = (spec, connection) => {{
                    if (spec.tooltip_mode === 'discord') {{
                        return `${{spec.node_label}} response: ${{formatTooltipLatency(connection.lastLatencyMs)}}\\nDiscord: ${{discordServiceText(connection.discordServiceState, connection.discordLatencyMs)}}`;
                    }}
                    if (spec.tooltip_mode === 'portal') {{
                        return specs
                            .filter((target) => target.tooltip_mode === 'discord')
                            .map((target) => `Portal → ${{target.node_label}}: ${{formatTooltipLatency(connection.portalNodeLatencies?.[target.node_name])}}; Discord: ${{discordServiceText(connection.discordServiceStates?.[target.node_name], connection.discordLatencies?.[target.node_name] ?? null)}}`)
                            .join('\\n') || spec.pending_text;
                    }}
                    return connection.lastText || spec.pending_text;
                }};
                const refreshTooltip = (nodeName) => {{
                    const connection = controllerState.connectionsByNode[nodeName];
                    const spec = getSpec(nodeName);
                    if (connection && spec) {{
                        setBadgeTooltip(spec, tooltipText(spec, connection));
                    }}
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
                const samplePortalNodeLatency = async (nodeName) => {{
                    const connection = controllerState.connectionsByNode[nodeName];
                    const spec = getSpec(nodeName);
                    if (!connection || !spec || !spec.portal_node_latencies_url) {{
                        return null;
                    }}
                    try {{
                        const response = await fetch(spec.portal_node_latencies_url, {{cache: 'no-store'}});
                        if (!response.ok) {{
                            return null;
                        }}
                        const payload = await response.json();
                        if (!payload || typeof payload !== 'object' || !payload.latencies || typeof payload.latencies !== 'object') {{
                            return null;
                        }}
                        connection.portalNodeLatencies = payload.latencies;
                        connection.discordServiceStates = payload.discord_service_states
                            && typeof payload.discord_service_states === 'object'
                            ? payload.discord_service_states
                            : {{}};
                        connection.discordLatencies = payload.discord_latencies
                            && typeof payload.discord_latencies === 'object'
                            ? payload.discord_latencies
                            : {{}};
                        const discordLatencyMs = payload.discord_latencies?.[nodeName];
                        connection.discordLatencyMs = typeof discordLatencyMs === 'number' && Number.isFinite(discordLatencyMs)
                            ? Math.max(0, Math.round(discordLatencyMs))
                            : null;
                        refreshTooltip(nodeName);
                        const latencyMs = payload.latencies[nodeName];
                        return typeof latencyMs === 'number' && Number.isFinite(latencyMs)
                            ? Math.max(1, Math.round(latencyMs))
                            : null;
                    }} catch (_error) {{
                        return null;
                    }}
                }};
                const sampleHttpLatency = async (url, connection) => {{
                    const abortController = new AbortController();
                    const timeoutHandle = window.setTimeout(() => abortController.abort(), latencyTimeoutMs);
                    const startedAt = performance.now();
                    try {{
                        const response = await fetch(url, {{
                            cache: 'no-store',
                            signal: abortController.signal,
                        }});
                        if (!response.ok) {{
                            return null;
                        }}
                        const rawDiscordLatencyMs = response.headers.get(discordLatencyHeader);
                        const rawDiscordServiceState = response.headers.get(discordServiceStateHeader);
                        const parsedDiscordLatencyMs = rawDiscordLatencyMs === null ? null : Number(rawDiscordLatencyMs);
                        connection.discordServiceState = rawDiscordServiceState === null ? null : rawDiscordServiceState;
                        connection.discordLatencyMs = typeof parsedDiscordLatencyMs === 'number'
                            && Number.isFinite(parsedDiscordLatencyMs)
                            && parsedDiscordLatencyMs >= 0
                            ? Math.round(parsedDiscordLatencyMs)
                            : null;
                        return Math.max(1, Math.round(performance.now() - startedAt));
                    }} catch (_error) {{
                        return null;
                    }} finally {{
                        window.clearTimeout(timeoutHandle);
                    }}
                }};
                const sampleLatencyMeasurement = async (nodeName) => {{
                    const connection = controllerState.connectionsByNode[nodeName];
                    const spec = getSpec(nodeName);
                    if (!connection || !spec || !spec.show_latency) {{
                        return null;
                    }}
                    if (spec.presence_health_url) {{
                        return await sampleHttpLatency(spec.presence_health_url, connection);
                    }}
                    if (spec.direct_latency_probe_url) {{
                        return await sampleHttpLatency(spec.direct_latency_probe_url, connection);
                    }}
                    if (spec.portal_node_latencies_url && !spec.presence_stream_url) {{
                        return await samplePortalNodeLatency(nodeName);
                    }}
                    if (!connection.socket || connection.socket.readyState !== WebSocket.OPEN) {{
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
                const confirmPresence = async (nodeName) => {{
                    const connection = controllerState.connectionsByNode[nodeName];
                    const spec = getSpec(nodeName);
                    if (!connection || !spec || !connection.socket || connection.socket.readyState !== WebSocket.OPEN) {{
                        return false;
                    }}
                    return await new Promise((resolve) => {{
                        const sampleId = `${{Date.now()}}-${{Math.random().toString(16).slice(2)}}`;
                        const timeoutHandle = window.setTimeout(() => {{
                            delete connection.pendingSamples[sampleId];
                            resolve(false);
                        }}, latencyTimeoutMs);
                        connection.pendingSamples[sampleId] = {{
                            timeoutHandle,
                            resolve: () => {{
                                window.clearTimeout(timeoutHandle);
                                delete connection.pendingSamples[sampleId];
                                resolve(true);
                            }},
                        }};
                        connection.socket.send(JSON.stringify({{type: 'ping', sample_id: sampleId}}));
                    }});
                }};
                const renderAliveState = async (nodeName, latencyMs) => {{
                    const connection = controllerState.connectionsByNode[nodeName];
                    const spec = getSpec(nodeName);
                    if (!connection || !spec) {{
                        return;
                    }}
                    connection.lastLatencyMs = latencyMs;
                    const formattedLatency = formatLatency(latencyMs);
                    const nextText = formattedLatency === null ? spec.alive_text : latencyText(spec, formattedLatency);
                    connection.lastText = nextText;
                    connection.lastClassName = spec.healthy_class_name;
                    renderBadge(spec, nextText, spec.healthy_class_name);
                    refreshTooltip(nodeName);
                }};
                const renderDownState = (nodeName) => {{
                    const connection = controllerState.connectionsByNode[nodeName];
                    const spec = getSpec(nodeName);
                    if (!connection || !spec) {{
                        return;
                    }}
                    connection.lastText = spec.down_text;
                    connection.lastClassName = spec.unhealthy_class_name;
                    renderBadge(spec, spec.down_text, spec.unhealthy_class_name);
                    refreshTooltip(nodeName);
                }};
                const requestPortalNodeLatencies = (nodeName) => {{
                    const connection = controllerState.connectionsByNode[nodeName];
                    const spec = getSpec(nodeName);
                    if (!connection || !spec || spec.tooltip_mode !== 'portal') {{
                        return;
                    }}
                    if (spec.portal_node_latencies_url) {{
                        if (connection.portalLatencyRequestInFlight) {{
                            return;
                        }}
                        connection.portalLatencyRequestInFlight = true;
                        void fetch(spec.portal_node_latencies_url, {{cache: 'no-store'}})
                            .then(async (response) => {{
                                if (!response.ok) {{
                                    return null;
                                }}
                                return await response.json();
                            }})
                            .then((payload) => {{
                                if (
                                    controllerState.connectionsByNode[nodeName] !== connection
                                    || !payload
                                    || typeof payload !== 'object'
                                    || payload.node !== nodeName
                                    || !payload.latencies
                                    || typeof payload.latencies !== 'object'
                                ) {{
                                    return;
                                }}
                                connection.portalNodeLatencies = payload.latencies;
                                connection.discordServiceStates = payload.discord_service_states
                                    && typeof payload.discord_service_states === 'object'
                                    ? payload.discord_service_states
                                    : {{}};
                                connection.discordLatencies = payload.discord_latencies
                                    && typeof payload.discord_latencies === 'object'
                                    ? payload.discord_latencies
                                    : {{}};
                                refreshTooltip(nodeName);
                            }})
                            .catch(() => {{}})
                            .finally(() => {{
                                connection.portalLatencyRequestInFlight = false;
                            }});
                        return;
                    }}
                    if (!connection.socket || connection.socket.readyState !== WebSocket.OPEN) {{
                        return;
                    }}
                    connection.socket.send(JSON.stringify({{type: 'node_latencies'}}));
                }};
                const beginLatencySample = (nodeName) => {{
                    const connection = controllerState.connectionsByNode[nodeName];
                    if (!connection || connection.latencySampleInFlight) {{
                        return null;
                    }}
                    connection.latencySampleInFlight = true;
                    return connection;
                }};
                const finishLatencySample = (nodeName, connection) => {{
                    if (controllerState.connectionsByNode[nodeName] === connection) {{
                        connection.latencySampleInFlight = false;
                    }}
                }};
                const runLatencyBootstrap = async (nodeName) => {{
                    const spec = getSpec(nodeName);
                    if (!spec || !spec.show_latency) {{
                        return;
                    }}
                    const connection = beginLatencySample(nodeName);
                    if (!connection) {{
                        return;
                    }}
                    try {{
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
                        if ((spec.presence_health_url || spec.direct_latency_probe_url || spec.portal_node_latencies_url) && summary === null) {{
                            renderDownState(nodeName);
                            return;
                        }}
                        await renderAliveState(nodeName, summary);
                        requestPortalNodeLatencies(nodeName);
                    }} finally {{
                        finishLatencySample(nodeName, connection);
                    }}
                }};
                const runLatencySample = async (nodeName) => {{
                    const spec = getSpec(nodeName);
                    if (!spec || !spec.show_latency) {{
                        return;
                    }}
                    const connection = beginLatencySample(nodeName);
                    if (!connection) {{
                        return;
                    }}
                    try {{
                        const latency = await sampleLatencyMeasurement(nodeName);
                        if ((spec.presence_health_url || spec.direct_latency_probe_url || spec.portal_node_latencies_url) && latency === null) {{
                            renderDownState(nodeName);
                            return;
                        }}
                        await renderAliveState(nodeName, latency);
                        requestPortalNodeLatencies(nodeName);
                    }} finally {{
                        finishLatencySample(nodeName, connection);
                    }}
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
                    if (!spec.presence_stream_url && !spec.direct_latency_probe_url && !spec.presence_health_url && !spec.portal_node_latencies_url) {{
                        renderBadge(spec, spec.pending_text, spec.pending_class_name);
                        closeConnection(nodeName);
                        return;
                    }}
                    const existingConnection = controllerState.connectionsByNode[nodeName];
                    if (existingConnection) {{
                        if (!existingConnection.socket || existingConnection.socket.readyState === WebSocket.OPEN || existingConnection.socket.readyState === WebSocket.CONNECTING) {{
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
                        lastLatencyMs: null,
                        discordLatencyMs: null,
                        discordServiceState: null,
                        discordServiceStates: {{}},
                        discordLatencies: {{}},
                        portalNodeLatencies: {{}},
                        portalLatencyRequestInFlight: false,
                        latencySampleInFlight: false,
                    }};
                    controllerState.connectionsByNode[nodeName] = connection;
                    renderBadge(spec, spec.pending_text, spec.pending_class_name);
                    if (spec.presence_health_url || spec.direct_latency_probe_url || (spec.portal_node_latencies_url && !spec.presence_stream_url)) {{
                        if (spec.show_latency) {{
                            void runLatencyBootstrap(nodeName);
                            connection.latencyIntervalId = window.setInterval(() => {{
                                void runLatencySample(nodeName);
                            }}, latencyRefreshIntervalMs);
                        }} else {{
                            void renderAliveState(nodeName, null);
                        }}
                        return;
                    }}
                    const socket = new WebSocket(websocketUrl(spec.presence_stream_url));
                    connection.socket = socket;
                    socket.addEventListener('open', () => {{
                        const latestSpec = getSpec(nodeName);
                        if (!latestSpec) {{
                            closeConnection(nodeName);
                            return;
                        }}
                        connection.lastText = latestSpec.pending_text;
                        connection.lastClassName = latestSpec.pending_class_name;
                        renderBadge(latestSpec, connection.lastText, connection.lastClassName);
                        refreshTooltip(nodeName);
                        rejectPendingSamples(connection);
                        if (connection.latencyIntervalId) {{
                            window.clearInterval(connection.latencyIntervalId);
                        }}
                        if (latestSpec.show_latency) {{
                            void runLatencyBootstrap(nodeName);
                            connection.latencyIntervalId = window.setInterval(() => {{
                                void runLatencySample(nodeName);
                            }}, latencyRefreshIntervalMs);
                        }} else {{
                            void confirmPresence(nodeName).then((confirmed) => {{
                                if (confirmed) {{
                                    void renderAliveState(nodeName, null);
                                }}
                            }});
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
                        if (payload.node !== nodeName) {{
                            socket.close();
                            return;
                        }}
                        if (payload.type === 'node_latencies') {{
                            if (payload.latencies && typeof payload.latencies === 'object') {{
                                connection.portalNodeLatencies = payload.latencies;
                                refreshTooltip(nodeName);
                            }}
                            return;
                        }}
                        if (typeof payload.discord_latency_ms === 'number' && Number.isFinite(payload.discord_latency_ms)) {{
                            connection.discordLatencyMs = payload.discord_latency_ms;
                        }}
                        if (typeof payload.discord_service_state === 'string') {{
                            connection.discordServiceState = payload.discord_service_state;
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
        connected_player_names: tuple[str, ...],
        fallback_text: str | None = None,
    ) -> str | None:
        if player_names_html := self._tooltip_lines_html(connected_player_names):
            return player_names_html
        if fallback_text is None:
            return None
        return self._tooltip_lines_html((fallback_text,))

    @staticmethod
    def _app_list_api_actions_enabled(request: Request) -> bool:
        value = request.query_params.get(_APP_LIST_API_QUERY_PARAM)
        if value is None:
            return False
        return value.strip().casefold() in {"1", "true", "yes", "on"}

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

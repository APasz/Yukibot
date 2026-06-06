from __future__ import annotations

from typing import TYPE_CHECKING

from .runtime_imports import (
    BadgeTone,
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
)
from .nicegui_protocols import ModWebUi

from .service_base import ModWebServiceSupport

if TYPE_CHECKING:
    from nicegui.element import Element

class ModWebUiHelpersMixin(ModWebServiceSupport):
    @staticmethod
    def _apply_theme(*, ui: ModWebUi) -> None:
        apply_mod_web_theme(ui=ui)

    @staticmethod
    def _badge(*, ui: ModWebUi, text: str, tone: BadgeTone, extra_classes: str = "") -> Label:
        return ui.label(text).classes(f"{mod_web_badge_class(tone)} {extra_classes}".strip())

    def _interactive_badge(
        self,
        *,
        ui: ModWebUi,
        text: str,
        tone: BadgeTone,
        url: str | None = None,
        tooltip_text: str | None = None,
        extra_classes: str = "",
    ) -> "Element":
        if url is None:
            badge = cast("Element", self._badge(ui=ui, text=text, tone=tone, extra_classes=extra_classes))
        else:
            badge = self._badge_link(ui=ui, text=text, tone=tone, url=url, extra_classes=extra_classes)
        if tooltip_text is not None:
            self._attach_text_tooltip(ui=ui, target=badge, text=tooltip_text)
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
    ) -> "Element":
        badge = ui.link(text, url).classes(
            f"{mod_web_badge_class(tone)} mod-badge-link cursor-pointer {extra_classes}".strip()
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
            if not node.is_current and node.node_name.strip().casefold() in requested_keys
        )

    def _toggle_simulated_down_node_url(
        self,
        *,
        current_url: str,
        node_name: str,
        simulated_down_node_names: tuple[str, ...],
    ) -> str:
        remote_node_names = tuple(node.node_name for node in self._node_links() if not node.is_current)
        remote_node_name_keys = {remote_name.casefold() for remote_name in remote_node_names}
        node_key = node_name.strip().casefold()
        if node_key not in remote_node_name_keys:
            raise ValueError(f"Cannot simulate current or unknown node as down: {node_name!r}")

        updated_keys = {remote_name.casefold() for remote_name in simulated_down_node_names}
        if node_key in updated_keys:
            updated_keys.remove(node_key)
        else:
            updated_keys.add(node_key)

        updated_node_names = tuple(
            remote_name for remote_name in remote_node_names if remote_name.casefold() in updated_keys
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

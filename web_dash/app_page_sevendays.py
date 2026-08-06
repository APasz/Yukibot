from __future__ import annotations

from .nicegui_protocols import ModWebUi
from .runtime_imports import ModWebUser, escape
from .service_base import ModWebServiceSupport
from .types import (
    ModWebAppTabDefinition,
    ModWebBasePageModel,
    ModWebSevenDaysSandboxOptionEntry,
    ModWebSevenDaysSandboxOptionsSummary,
    _ModWebBadgeSpec,
)


class ModWebAppPageSevenDaysMixin(ModWebServiceSupport):
    @staticmethod
    def _sevendays_sandbox_options_tab_badges(
        *,
        model: ModWebBasePageModel,
        user: ModWebUser,
        tab: ModWebAppTabDefinition,
    ) -> tuple[_ModWebBadgeSpec, ...]:
        del user, tab
        summary = model.sevendays_sandbox_options
        if summary is None:
            return ()
        if summary.load_error is not None:
            return (_ModWebBadgeSpec(text="Load error", tone="warn"),)
        return ()

    def _render_sevendays_sandbox_options_section(
        self,
        *,
        ui: ModWebUi,
        model: ModWebBasePageModel,
        user: ModWebUser,
        tab: ModWebAppTabDefinition,
    ) -> None:
        del user, tab
        summary = model.sevendays_sandbox_options
        if summary is None:
            self._render_flat_tab_empty_state(
                ui=ui,
                title="Sandbox settings",
                description="Sandbox settings are not available yet.",
                detail_text="Start the server, then use `Get Sandbox Options` from the Console tab to load its active rules.",
            )
            return
        if summary.load_error is not None:
            self._render_flat_tab_empty_state(
                ui=ui,
                title="Sandbox settings",
                description="Sandbox settings could not be loaded.",
                detail_text=summary.load_error,
            )
            return
        if not summary.options:
            self._render_flat_tab_empty_state(
                ui=ui,
                title="Sandbox settings",
                description="No sandbox settings have been received yet.",
                detail_text="Use `Get Sandbox Options` from the Console tab while the server is running.",
            )
            return

        option_count = len(summary.options)
        section_count = len({option.section.casefold() for option in summary.options})
        with ui.card().classes(self._flat_tab_card_classes()):
            with ui.column().classes(self._tab_section_body_classes()):
                self._render_flat_tab_header(
                    ui=ui,
                    title="Sandbox settings",
                )
                ui.label(f"{option_count} settings in {section_count} categories").classes("mod-subtitle text-sm")
                if summary.sandbox_code is not None or summary.generated_at is not None:
                    with ui.element("div").classes("grid grid-cols-1 md:grid-cols-2 gap-3 w-full"):
                        if summary.sandbox_code is not None:
                            self._render_sevendays_sandbox_summary_value(
                                ui=ui,
                                label="Sandbox code",
                                value=summary.sandbox_code,
                            )
                        if summary.generated_at is not None:
                            self._render_sevendays_sandbox_summary_value(
                                ui=ui,
                                label="Last updated",
                                value=summary.generated_at,
                            )
        ui.html(self._sevendays_sandbox_options_markup(summary)).classes("w-full")

    @staticmethod
    def _render_sevendays_sandbox_summary_value(*, ui: ModWebUi, label: str, value: str) -> None:
        with ui.card().classes("mod-card w-full"):
            with ui.column().classes("w-full gap-1"):
                ui.label(label).classes("text-[0.68rem] uppercase tracking-[0.18em] mod-subtitle")
                ui.label(value).classes("text-sm font-black mod-title-small break-all")

    @classmethod
    def _sevendays_sandbox_options_markup(cls, summary: ModWebSevenDaysSandboxOptionsSummary | None) -> str:
        if summary is None:
            return (
                '<div class="mod-card mod-card-empty mod-card-plain">'
                '<div class="mod-subtitle">Sandbox option data is not available for this node yet.</div>'
                "</div>"
            )
        if summary.load_error is not None:
            return (
                '<div class="mod-card mod-card-empty mod-card-plain">'
                '<div class="mod-subtitle">Sandbox option data could not be loaded.</div>'
                f'<div class="mod-subtitle">{escape(summary.load_error)}</div>'
                "</div>"
            )
        if not summary.options:
            return (
                '<div class="mod-card mod-card-empty mod-card-plain">'
                '<div class="mod-subtitle">No sandbox options have been parsed yet.</div>'
                "</div>"
            )
        grouped_options: dict[str, list[ModWebSevenDaysSandboxOptionEntry]] = {}
        for option in summary.options:
            grouped_options.setdefault(option.section, []).append(option)
        section_markup = "".join(
            cls._sevendays_sandbox_option_section_markup(section=section, options=tuple(options))
            for section, options in sorted(grouped_options.items(), key=lambda item: item[0].casefold())
        )
        return f'<div class="mod-sandbox-options">{section_markup}</div>'

    @staticmethod
    def _sevendays_sandbox_option_section_markup(
        *,
        section: str,
        options: tuple[ModWebSevenDaysSandboxOptionEntry, ...],
    ) -> str:
        rows = "".join(
            (
                '<div class="mod-sandbox-option">'
                '<div class="mod-sandbox-option-name">'
                f'<div class="mod-title">{escape(option.key)}</div>'
                "</div>"
                '<div class="mod-sandbox-option-values">'
                f'<div class="mod-sandbox-option-current">{escape(option.value_label)}</div>'
                + (
                    f'<div class="mod-sandbox-option-default">Default · {escape(option.default_label)}</div>'
                    if (option.value_index, option.value_label) != (option.default_index, option.default_label)
                    else ""
                )
                + "</div>"
                + "</div>"
            )
            for option in options
        )
        return (
            '<section class="mod-card mod-sandbox-section">'
            '<div class="mod-sandbox-section-header">'
            f'<div class="mod-title">{escape(section)}</div>'
            f'<div class="mod-sandbox-section-count">{len(options)} setting{"s" if len(options) != 1 else ""}</div>'
            "</div>"
            f"{rows}"
            "</section>"
        )

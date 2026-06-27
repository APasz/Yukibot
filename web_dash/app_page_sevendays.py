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
        return (
            _ModWebBadgeSpec(text=f"{len(summary.options)} options", tone="black" if summary.options else "grey"),
            _ModWebBadgeSpec(text="3.0 b259+", tone="grey"),
        )

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
        data_path = self._sevendays_sandbox_data_path(summary)
        file_status = self._sevendays_sandbox_file_status(summary)
        option_count = 0 if summary is None else len(summary.options)
        section_count = 0 if summary is None else len({option.section.casefold() for option in summary.options})
        generated_at = "Unknown" if summary is None or summary.generated_at is None else summary.generated_at
        app_version = "Unknown" if summary is None or summary.app_version is None else summary.app_version
        sandbox_code = "Unavailable" if summary is None or summary.sandbox_code is None else summary.sandbox_code

        with ui.card().classes(self._flat_tab_card_classes()):
            with ui.column().classes(self._tab_section_body_classes()):
                self._render_flat_tab_header(
                    ui=ui,
                    title="Sandbox",
                    description="Review the persisted 7D2D sandbox option snapshot detected by Yukibot.",
                )
                with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                    self._badge(ui=ui, text=file_status, tone=self._sevendays_sandbox_status_tone(summary))
                    self._badge(ui=ui, text=f"{option_count} options", tone="black" if option_count else "grey")
                    self._badge(ui=ui, text=f"{section_count} sections", tone="grey")
                with ui.element("div").classes("grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-3 w-full"):
                    self._render_sevendays_sandbox_summary_value(ui=ui, label="Detected version", value=app_version)
                    self._render_sevendays_sandbox_summary_value(ui=ui, label="Generated", value=generated_at)
                    self._render_sevendays_sandbox_summary_value(ui=ui, label="Sandbox code", value=sandbox_code)
                    self._render_sevendays_sandbox_summary_value(ui=ui, label="Snapshot file", value=data_path)

        if summary is None:
            self._render_flat_tab_empty_state(
                ui=ui,
                title="Sandbox",
                description="Sandbox option data is not available for this node yet.",
                detail_text="Run `Get Sandbox Options` while the server is running to populate the persisted snapshot.",
                secondary_description=f"Expected file: {data_path}",
            )
            return
        if summary.load_error is not None:
            self._render_flat_tab_empty_state(
                ui=ui,
                title="Sandbox",
                description="Sandbox option data could not be loaded.",
                detail_text=summary.load_error,
            )
            return
        if not summary.options:
            self._render_flat_tab_empty_state(
                ui=ui,
                title="Sandbox",
                description="No sandbox options have been parsed yet.",
                detail_text="The snapshot exists, but it does not currently contain any parsed option entries.",
            )
            return
        ui.html(self._sevendays_sandbox_options_markup(summary)).classes("w-full")

    @staticmethod
    def _sevendays_sandbox_status_tone(summary: ModWebSevenDaysSandboxOptionsSummary | None) -> str:
        if summary is None or not summary.file_exists:
            return "grey"
        if summary.load_error is not None:
            return "warn"
        if not summary.options:
            return "grey"
        return "black"

    @staticmethod
    def _sevendays_sandbox_file_status(summary: ModWebSevenDaysSandboxOptionsSummary | None) -> str:
        if summary is None or not summary.file_exists:
            return "Snapshot missing"
        if summary.load_error is not None:
            return "Snapshot unreadable"
        if not summary.options:
            return "Snapshot empty"
        return "Snapshot ready"

    @staticmethod
    def _sevendays_sandbox_data_path(summary: ModWebSevenDaysSandboxOptionsSummary | None) -> str:
        if summary is None:
            return ".yukibot/sandbox_options.json"
        return summary.data_path

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
        return f'<div class="mod-config-file-list">{section_markup}</div>'

    @staticmethod
    def _sevendays_sandbox_option_section_markup(
        *,
        section: str,
        options: tuple[ModWebSevenDaysSandboxOptionEntry, ...],
    ) -> str:
        rows = "".join(
            (
                '<div class="mod-config-file-row">'
                '<div class="min-w-0">'
                f'<div class="mod-title">{escape(option.key)}</div>'
                f'<div class="mod-subtitle">Current index <code>{escape(str(option.value_index))}</code> · '
                f"Default index <code>{escape(str(option.default_index))}</code></div>"
                "</div>"
                '<div class="mod-config-file-meta">'
                f'<span class="mod-pill">{escape(str(option.value_index))}/{escape(option.value_label)}</span>'
                f'<span class="mod-pill">{escape(str(option.default_index))}/{escape(option.default_label)}</span>'
                "</div>"
                "</div>"
            )
            for option in options
        )
        return (
            '<div class="mod-card mod-card-plain">'
            '<div class="mod-config-file-body">'
            f'<div class="mod-title">{escape(section)}</div>'
            f'<div class="mod-subtitle">{len(options)} option{"s" if len(options) != 1 else ""}</div>'
            f"{rows}"
            "</div>"
            "</div>"
        )

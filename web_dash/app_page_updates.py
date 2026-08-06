from __future__ import annotations

from dataclasses import dataclass

from .constants import log
from .nicegui_protocols import ModWebNotificationType, ModWebUi, ModWebValueContainer, _value_as_text
from .runtime_imports import (
    AppUpdateInfo,
    AppUpdateOperationKind,
    AppUpdateState,
    AppUpdateStatus,
    Awaitable,
    BadgeTone,
    Callable,
    ModWebUser,
    NodeAppMutationAction,
    NodeAppRuntimeSummary,
    SteamUpdatePreset,
    app_scope_from_name,
    config,
    escape,
    replace,
    required_app_mutation_level,
    steam_update_preset_for_scope,
    time,
)
from .service_base import ModWebServiceSupport
from .types import ModWebBasePageModel, _ModWebBadgeSpec


@dataclass(frozen=True, slots=True)
class _UpdateSectionViewState:
    target_branch_label: str
    configured_branch_text: str
    pending_branch_text: str | None
    branch_selection_disabled: bool
    dry_preview_active: bool
    installed_version: str
    installed_branch: str
    manifest_build: str
    provider_label: str
    app_id: int | None
    install_alignment_badge: _ModWebBadgeSpec
    status: AppUpdateStatus | None
    status_summary: str
    status_detail: str | None
    progress_percent: float | None
    progress_text: str
    update_block_reason: str | None
    verify_block_reason: str | None
    update_button_label: str
    verify_button_label: str
    action_status_text: str
    status_title: str
    show_log: bool


class ModWebAppPageUpdateMixin(ModWebServiceSupport):
    @staticmethod
    def _update_section_badges(model: ModWebBasePageModel) -> tuple[_ModWebBadgeSpec, ...]:
        update_info = model.update_info
        if update_info is None:
            return ()
        badges: list[_ModWebBadgeSpec] = [
            _ModWebBadgeSpec(text=update_info.selected_branch_label, tone="black"),
        ]
        update_status = model.update_status
        if update_status is not None:
            badges.append(
                _ModWebBadgeSpec(
                    text=ModWebAppPageUpdateMixin._update_status_badge_text(update_status),
                    tone=ModWebAppPageUpdateMixin._update_status_badge_tone(update_status),
                )
            )
        return tuple(badges)

    @staticmethod
    def _details_steam_update_preset(app_name: str) -> SteamUpdatePreset | None:
        return steam_update_preset_for_scope(app_scope_from_name(app_name))

    @classmethod
    def _details_steam_update_branch_options(
        cls,
        *,
        app_name: str,
        update_info: AppUpdateInfo | None,
    ) -> dict[str, str]:
        if update_info is not None:
            return {branch.branch_id: f"{branch.label} ({branch.branch_id})" for branch in update_info.branches}
        preset = cls._details_steam_update_preset(app_name)
        if preset is None:
            return {}
        return {branch.branch_id: f"{branch.display_label} ({branch.branch_id})" for branch in preset.branches}

    @classmethod
    def _details_steam_update_selected_branch(
        cls,
        *,
        app_name: str,
        update_info: AppUpdateInfo | None,
    ) -> str | None:
        if update_info is not None:
            return update_info.selected_branch_id
        preset = cls._details_steam_update_preset(app_name)
        if preset is None:
            return None
        return preset.default_selected_branch

    @classmethod
    def _details_steam_update_app_id(
        cls,
        *,
        app_name: str,
        update_info: AppUpdateInfo | None,
    ) -> int | None:
        if update_info is not None and update_info.app_id is not None:
            return update_info.app_id
        preset = cls._details_steam_update_preset(app_name)
        if preset is None:
            return None
        return preset.app_id

    @staticmethod
    def _update_status_badge_text(status: AppUpdateStatus) -> str:
        if status.state is AppUpdateState.RUNNING and status.operation_kind is not None:
            return status.operation_kind.value.title()
        if status.state is AppUpdateState.SUCCEEDED:
            return "Ready"
        return status.state.value.title()

    @staticmethod
    def _update_status_badge_tone(status: AppUpdateStatus) -> BadgeTone:
        if status.state is AppUpdateState.RUNNING:
            return "purple"
        if status.state is AppUpdateState.FAILED:
            return "red"
        if status.state is AppUpdateState.SUCCEEDED:
            return "black"
        return "grey"

    @staticmethod
    def _update_log_markup(status: AppUpdateStatus) -> str:
        if not status.log_lines:
            return "<pre class='mod-update-log'>Waiting for SteamCMD output...</pre>"
        log_html = "\n".join(escape(line) for line in status.log_lines)
        return f"<pre class='mod-update-log'>{log_html}</pre>"

    @staticmethod
    def _resolve_update_target_branch_id(update_info: AppUpdateInfo, selected_branch_id: str) -> str:
        selected_branch_key: str = selected_branch_id.strip().casefold()
        if not selected_branch_key:
            return update_info.selected_branch_id
        for branch in update_info.branches:
            if branch.branch_id.casefold() == selected_branch_key:
                return branch.branch_id
        return update_info.selected_branch_id

    @classmethod
    def _pending_update_target_branch_id(
        cls,
        update_info: AppUpdateInfo,
        selected_branch_id: str,
    ) -> str | None:
        target_branch_id = cls._resolve_update_target_branch_id(update_info, selected_branch_id)
        if target_branch_id.casefold() == update_info.selected_branch_id.casefold():
            return None
        return target_branch_id

    @classmethod
    def _update_branch_display_text(cls, update_info: AppUpdateInfo, branch_id: str) -> str:
        resolved_branch_id = cls._resolve_update_target_branch_id(update_info, branch_id)
        if resolved_branch_id.casefold() == update_info.selected_branch_id.casefold():
            return f"{update_info.selected_branch_label} ({update_info.selected_branch_id})"
        for branch in update_info.branches:
            if branch.branch_id.casefold() == resolved_branch_id.casefold():
                return f"{branch.label} ({branch.branch_id})"
        return f"{update_info.selected_branch_label} ({update_info.selected_branch_id})"

    @classmethod
    def _pending_update_target_display_text(cls, update_info: AppUpdateInfo, selected_branch_id: str) -> str:
        pending_branch_id = cls._pending_update_target_branch_id(update_info, selected_branch_id)
        if pending_branch_id is None:
            return "No pending change"
        return cls._update_branch_display_text(update_info, pending_branch_id)

    @staticmethod
    def _update_progress_percent(status: AppUpdateStatus | None) -> float | None:
        if status is None or status.progress_percent is None:
            return None
        return min(100.0, max(0.0, status.progress_percent))

    @classmethod
    def _update_progress_text(cls, status: AppUpdateStatus | None) -> str:
        progress_percent = cls._update_progress_percent(status)
        if progress_percent is None:
            return "Progress unavailable"
        return f"{progress_percent:.2f}%"

    @staticmethod
    def _update_action_block_reason(
        *,
        action: NodeAppMutationAction,
        can_manage_updates: bool,
        app_running: bool,
        update_running: bool,
        supports_verify: bool,
    ) -> str | None:
        if not can_manage_updates:
            return "Requires Sudo access."
        if update_running:
            return "Another update operation is already running."
        if app_running:
            return "Stop the app to update or verify."
        if action is NodeAppMutationAction.VERIFY and not supports_verify:
            return "Verification is not available for this update provider."
        return None

    @staticmethod
    def _update_branch_ids_match(left_branch_id: str | None, right_branch_id: str | None) -> bool:
        if left_branch_id is None or right_branch_id is None:
            return False
        return left_branch_id.strip().casefold() == right_branch_id.strip().casefold()

    @classmethod
    def _update_install_alignment_badge(
        cls,
        update_info: AppUpdateInfo,
    ) -> _ModWebBadgeSpec:
        installed_branch_id = update_info.installed_branch_id
        if installed_branch_id is None:
            return _ModWebBadgeSpec(text="Manifest branch unknown", tone="grey")
        if cls._update_branch_ids_match(installed_branch_id, update_info.selected_branch_id):
            return _ModWebBadgeSpec(text="Installed matches configured target", tone="black")
        return _ModWebBadgeSpec(text="Installed differs from configured target", tone="purple")

    @staticmethod
    def _update_section_runtime_signature(app_stats: NodeAppRuntimeSummary | None) -> tuple[bool | None, str | None]:
        if app_stats is None:
            return (None, None)
        return (app_stats.running, app_stats.version)

    @classmethod
    def _update_section_view_signature(
        cls,
        model: ModWebBasePageModel,
    ) -> tuple[AppUpdateInfo | None, AppUpdateStatus | None, tuple[bool | None, str | None]]:
        return (
            model.update_info,
            model.update_status,
            cls._update_section_runtime_signature(model.app_stats),
        )

    @classmethod
    def _dry_update_preview_statuses(cls) -> tuple[AppUpdateStatus, ...]:
        now_unix_ms = cls._update_unix_ms_now()
        return (
            AppUpdateStatus(
                state=AppUpdateState.RUNNING,
                summary="Downloading depot update",
                operation_kind=AppUpdateOperationKind.UPDATE,
                progress_percent=42.5,
                detail="Downloading package 3 of 7.",
                log_lines=(
                    "stdout: Connecting anonymously to Steam public...",
                    "stdout: Update state (0x61) downloading, progress: 42.50",
                    "stdout: Downloading depot 294421 chunk 3/7",
                ),
                started_at_unix_ms=now_unix_ms - 18_000,
            ),
            AppUpdateStatus(
                state=AppUpdateState.SUCCEEDED,
                summary="Updated preview app on Steam branch Experimental to 1.2.3 [Steam latest_experimental build 99999999].",
                operation_kind=AppUpdateOperationKind.UPDATE,
                progress_percent=100.0,
                detail="SteamCMD completed successfully.",
                log_lines=(
                    "stdout: Update state (0x81) verifying update, progress: 100.00",
                    "stdout: Success! App '123456' fully installed.",
                ),
                started_at_unix_ms=now_unix_ms - 95_000,
                finished_at_unix_ms=now_unix_ms - 4_000,
            ),
            AppUpdateStatus(
                state=AppUpdateState.FAILED,
                summary="Update failed for preview app.",
                operation_kind=AppUpdateOperationKind.UPDATE,
                progress_percent=63.0,
                detail="Command failed: steamcmd +app_update 123456 -beta latest_experimental [ERROR! Failed to install app]",
                log_lines=(
                    "stdout: Update state (0x61) downloading, progress: 63.00",
                    "stderr: ERROR! Failed to install app '123456' (disk write failure)",
                ),
                started_at_unix_ms=now_unix_ms - 71_000,
                finished_at_unix_ms=now_unix_ms - 2_000,
            ),
            AppUpdateStatus(
                state=AppUpdateState.SUCCEEDED,
                summary="Verified preview app on Steam branch Stable. Steam build 88888888.",
                operation_kind=AppUpdateOperationKind.VERIFY,
                progress_percent=100.0,
                detail="Validation completed with no missing files.",
                log_lines=(
                    "stdout: Update state (0x81) verifying update, progress: 100.00",
                    "stdout: Success! Verified installation of app '123456'.",
                ),
                started_at_unix_ms=now_unix_ms - 143_000,
                finished_at_unix_ms=now_unix_ms - 15_000,
            ),
            AppUpdateStatus(
                state=AppUpdateState.FAILED,
                summary="Verify failed for preview app.",
                operation_kind=AppUpdateOperationKind.VERIFY,
                progress_percent=88.0,
                detail="1 file failed validation and will be reacquired.",
                log_lines=(
                    "stdout: Update state (0x81) verifying update, progress: 88.00",
                    "stderr: Validation failure: 1 file corrupt or missing",
                ),
                started_at_unix_ms=now_unix_ms - 121_000,
                finished_at_unix_ms=now_unix_ms - 6_000,
            ),
        )

    @staticmethod
    def _format_update_timestamp(unix_ms: int) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(unix_ms / 1000))

    @staticmethod
    def _format_update_duration(*, started_at_unix_ms: int, finished_at_unix_ms: int | None = None) -> str:
        end_unix_ms = (
            ModWebAppPageUpdateMixin._update_unix_ms_now()
            if finished_at_unix_ms is None
            else finished_at_unix_ms
        )
        total_seconds = max(0, (end_unix_ms - started_at_unix_ms) // 1000)
        if total_seconds < 60:
            return f"{total_seconds}s"
        minutes, seconds = divmod(total_seconds, 60)
        if minutes < 60:
            return f"{minutes}m {seconds}s"
        hours, minutes = divmod(minutes, 60)
        if hours < 24:
            return f"{hours}h {minutes}m"
        days, hours = divmod(hours, 24)
        return f"{days}d {hours}h {minutes}m"

    @staticmethod
    def _update_status_sort_key(status: AppUpdateStatus) -> tuple[int, int, int]:
        return (
            -1 if status.started_at_unix_ms is None else status.started_at_unix_ms,
            -1 if status.finished_at_unix_ms is None else status.finished_at_unix_ms,
            1 if status.running else 0,
        )

    @classmethod
    def _prefer_newer_update_status(
        cls,
        current_status: AppUpdateStatus | None,
        next_status: AppUpdateStatus | None,
    ) -> AppUpdateStatus | None:
        if current_status is None:
            return next_status
        if next_status is None:
            return current_status
        if cls._update_status_sort_key(next_status) >= cls._update_status_sort_key(current_status):
            return next_status
        return current_status

    @classmethod
    def _merge_update_section_model(
        cls,
        current_model: ModWebBasePageModel,
        next_model: ModWebBasePageModel,
    ) -> ModWebBasePageModel:
        return replace(
            next_model,
            update_info=next_model.update_info if next_model.update_info is not None else current_model.update_info,
            update_status=cls._prefer_newer_update_status(current_model.update_status, next_model.update_status),
        )

    @classmethod
    def _update_branch_label(cls, update_info: AppUpdateInfo, branch_id: str) -> str:
        resolved_branch_id = cls._resolve_update_target_branch_id(update_info, branch_id)
        if resolved_branch_id.casefold() == update_info.selected_branch_id.casefold():
            return update_info.selected_branch_label
        for branch in update_info.branches:
            if branch.branch_id.casefold() == resolved_branch_id.casefold():
                return branch.label
        return update_info.selected_branch_label

    @staticmethod
    def _update_action_status_text(
        *,
        update_block_reason: str | None,
        verify_block_reason: str | None,
    ) -> str:
        if update_block_reason is not None:
            return update_block_reason
        if verify_block_reason is not None:
            return f"Update ready. {verify_block_reason}"
        return "Ready to update or verify."

    @staticmethod
    def _update_status_title(status: AppUpdateStatus | None) -> str:
        if status is None or status.state is not AppUpdateState.FAILED:
            return "Current operation" if status is not None and status.running else "Latest result"
        if status.operation_kind is None:
            return "Update failed"
        return f"{status.operation_kind.value.title()} failed"

    @classmethod
    def _update_section_view_state(
        cls,
        *,
        model: ModWebBasePageModel,
        update_info: AppUpdateInfo,
        status: AppUpdateStatus | None,
        selected_branch_id: str,
        can_manage_updates: bool,
        dry_preview_active: bool,
    ) -> _UpdateSectionViewState:
        resolved_selected_branch_id = cls._resolve_update_target_branch_id(update_info, selected_branch_id)
        target_branch_label = cls._update_branch_label(update_info, resolved_selected_branch_id)
        configured_branch_text = cls._update_branch_display_text(update_info, update_info.selected_branch_id)
        pending_branch_text = cls._pending_update_target_display_text(update_info, selected_branch_id)
        pending_branch_id = cls._pending_update_target_branch_id(update_info, selected_branch_id)
        app_running = model.app_stats is not None and model.app_stats.running
        update_running = status.running if status is not None else False
        update_block_reason = cls._update_action_block_reason(
            action=NodeAppMutationAction.UPDATE,
            can_manage_updates=can_manage_updates,
            app_running=app_running,
            update_running=update_running,
            supports_verify=update_info.supports_verify,
        )
        verify_block_reason = cls._update_action_block_reason(
            action=NodeAppMutationAction.VERIFY,
            can_manage_updates=can_manage_updates,
            app_running=app_running,
            update_running=update_running,
            supports_verify=update_info.supports_verify,
        )
        failed_operation = (
            status.operation_kind if status is not None and status.state is AppUpdateState.FAILED else None
        )
        update_button_prefix = "Retry update" if failed_operation is AppUpdateOperationKind.UPDATE else "Update"
        verify_button_label = "Retry Verify" if failed_operation is AppUpdateOperationKind.VERIFY else "Verify"
        installed_version = (
            "Unknown" if model.app_stats is None or model.app_stats.version is None else model.app_stats.version
        )
        installed_branch = update_info.installed_branch_id or "Manifest branch unavailable"
        manifest_build = "Unknown" if update_info.installed_build_id is None else str(update_info.installed_build_id)
        progress_percent = cls._update_progress_percent(status)
        return _UpdateSectionViewState(
            target_branch_label=target_branch_label,
            configured_branch_text=configured_branch_text,
            pending_branch_text=None if pending_branch_id is None else pending_branch_text,
            branch_selection_disabled=not can_manage_updates or update_running,
            dry_preview_active=dry_preview_active,
            installed_version=installed_version,
            installed_branch=installed_branch,
            manifest_build=manifest_build,
            provider_label=update_info.provider_label,
            app_id=update_info.app_id,
            install_alignment_badge=cls._update_install_alignment_badge(update_info),
            status=status,
            status_summary="No activity yet." if status is None else status.summary,
            status_detail=None if status is None else status.detail,
            progress_percent=progress_percent,
            progress_text=cls._update_progress_text(status),
            update_block_reason=update_block_reason,
            verify_block_reason=verify_block_reason,
            update_button_label=f"{update_button_prefix} to {target_branch_label}",
            verify_button_label=verify_button_label,
            action_status_text=cls._update_action_status_text(
                update_block_reason=update_block_reason,
                verify_block_reason=verify_block_reason,
            ),
            status_title=cls._update_status_title(status),
            show_log=status is not None and (bool(status.log_lines) or status.state is AppUpdateState.FAILED),
        )

    @staticmethod
    def _render_update_value(
        *,
        ui: ModWebUi,
        label: str,
        value: str,
        break_value: bool = False,
    ) -> None:
        value_classes = "text-sm font-black mod-title-small"
        if break_value:
            value_classes = f"{value_classes} break-all"
        with ui.column().classes("min-w-0 gap-1"):
            ui.label(label).classes("text-[0.68rem] uppercase tracking-[0.18em] mod-subtitle")
            ui.label(value).classes(value_classes)

    def _render_update_controls_card(
        self,
        *,
        ui: ModWebUi,
        view_state: _UpdateSectionViewState,
        branch_options: dict[str, str],
        selected_branch_id: str,
        on_branch_change: Callable[[ModWebValueContainer], None],
        on_update: Callable[[], Awaitable[None]],
        on_verify: Callable[[], Awaitable[None]],
        on_refresh: Callable[[], Awaitable[None]] | None,
        on_dry_preview: Callable[[], None] | None,
    ) -> None:
        with ui.card().classes("mod-card w-full"):
            with ui.column().classes("w-full gap-3"):
                ui.label(f"Update to {view_state.target_branch_label}").classes("text-sm font-black mod-title-small")
                branch_select = (
                    ui.select(
                        branch_options,
                        value=selected_branch_id,
                        label="Target branch",
                        on_change=on_branch_change,
                    )
                    .props("filled square dense hide-bottom-space color=accent options-dark")
                    .classes("mod-app-details-field mod-update-toolbar-branch")
                )
                if view_state.branch_selection_disabled:
                    branch_select.disable()
                with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                    self._badge(ui=ui, text=f"Active {view_state.configured_branch_text}", tone="black")
                    if view_state.pending_branch_text is not None:
                        self._badge(ui=ui, text=f"Pending {view_state.pending_branch_text}", tone="purple")
                    if view_state.dry_preview_active:
                        self._badge(ui=ui, text="Dry preview active", tone="grey")
                with ui.row().classes("w-full gap-2 flex-wrap"):
                    if on_dry_preview is not None:
                        ui.button("Dry", on_click=on_dry_preview).classes(
                            "mod-list-button secondary mod-toolbar-button"
                        )
                    update_button = ui.button(view_state.update_button_label, on_click=on_update).classes(
                        "mod-list-button mod-toolbar-button"
                    )
                    verify_button = ui.button(view_state.verify_button_label, on_click=on_verify).classes(
                        "mod-list-button secondary mod-toolbar-button"
                    )
                    if on_refresh is not None:
                        ui.button("Refresh", on_click=on_refresh).classes(
                            "mod-list-button secondary mod-toolbar-button"
                        )
                    if view_state.update_block_reason is not None:
                        update_button.disable()
                    if view_state.verify_block_reason is not None:
                        verify_button.disable()
                ui.label(view_state.action_status_text).classes("mod-subtitle text-sm")

    def _render_update_status_card(self, *, ui: ModWebUi, view_state: _UpdateSectionViewState) -> None:
        status = view_state.status
        with ui.card().classes("mod-card w-full xl:col-span-7"):
            with ui.column().classes("w-full gap-3"):
                ui.label(view_state.status_title).classes("text-sm font-black mod-title-small")
                with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                    if status is None:
                        self._badge(ui=ui, text="No attempt recorded", tone="grey")
                    else:
                        self._badge(
                            ui=ui,
                            text=self._update_status_badge_text(status),
                            tone=self._update_status_badge_tone(status),
                        )
                        if status.operation_kind is not None:
                            self._badge(ui=ui, text=status.operation_kind.value.title(), tone="black")
                        self._badge(ui=ui, text=view_state.progress_text, tone="grey")
                with ui.column().classes("w-full gap-2"):
                    ui.label(view_state.status_summary).classes("text-sm font-black mod-title-small break-all")
                    if view_state.status_detail is not None:
                        ui.label(view_state.status_detail).classes("mod-subtitle text-sm break-all")
                    with ui.element("div").classes("w-full border border-white/10 bg-black/40 h-3 overflow-hidden"):
                        ui.element("div").classes("h-full bg-white/70").style(
                            f"width: {0.0 if view_state.progress_percent is None else view_state.progress_percent:.2f}%;"
                        )
                    with ui.element("div").classes("grid grid-cols-1 md:grid-cols-3 gap-3 w-full"):
                        self._render_update_value(ui=ui, label="Progress", value=view_state.progress_text)
                        self._render_update_value(
                            ui=ui,
                            label="Started",
                            value=(
                                self._format_update_timestamp(status.started_at_unix_ms)
                                if status is not None and status.started_at_unix_ms is not None
                                else "No start recorded"
                            ),
                        )
                        self._render_update_value(
                            ui=ui,
                            label="Duration",
                            value=(
                                self._format_update_duration(
                                    started_at_unix_ms=status.started_at_unix_ms,
                                    finished_at_unix_ms=status.finished_at_unix_ms,
                                )
                                if status is not None and status.started_at_unix_ms is not None
                                else "Unavailable"
                            ),
                        )

    def _render_update_installed_card(self, *, ui: ModWebUi, view_state: _UpdateSectionViewState) -> None:
        with ui.card().classes("mod-card w-full xl:col-span-5"):
            with ui.column().classes("w-full gap-3"):
                ui.label("Installed").classes("text-sm font-black mod-title-small")
                with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                    self._badge(
                        ui=ui,
                        text=view_state.install_alignment_badge.text,
                        tone=view_state.install_alignment_badge.tone,
                    )
                    self._badge(ui=ui, text=view_state.provider_label, tone="grey")
                    if view_state.app_id is not None:
                        self._badge(ui=ui, text=f"App {view_state.app_id}", tone="grey")
                with ui.element("div").classes("grid grid-cols-1 md:grid-cols-2 gap-3 w-full"):
                    self._render_update_value(
                        ui=ui,
                        label="Installed version",
                        value=view_state.installed_version,
                        break_value=True,
                    )
                    self._render_update_value(
                        ui=ui,
                        label="Installed branch",
                        value=view_state.installed_branch,
                        break_value=True,
                    )
                    self._render_update_value(ui=ui, label="Manifest build", value=view_state.manifest_build)

    def _render_update_log(self, *, ui: ModWebUi, view_state: _UpdateSectionViewState) -> None:
        status = view_state.status
        if status is None or not view_state.show_log:
            return
        with ui.card().classes("mod-card w-full"):
            log_details = ui.element("details").classes("mod-update-log-details w-full")
            if status.state is AppUpdateState.FAILED:
                log_details.props("open")
            with log_details:
                with ui.element("summary").classes("mod-update-log-summary"):
                    line_label = "No output captured" if not status.log_lines else f"SteamCMD log · {len(status.log_lines)} lines"
                    ui.label(line_label).classes("mod-subtitle text-sm")
                if status.log_lines:
                    ui.html(self._update_log_markup(status)).classes(
                        "w-full border border-white/10 bg-black/70 p-3 text-xs max-h-96 overflow-auto"
                    )

    def _render_update_section(
        self,
        *,
        ui: ModWebUi,
        model: ModWebBasePageModel,
        user: ModWebUser,
        refresh_async_runtime_model: Callable[[], Awaitable[ModWebBasePageModel]] | None = None,
    ) -> Callable[[ModWebBasePageModel], None] | None:
        update_info = model.update_info
        if update_info is None:
            self._render_flat_tab_empty_state(
                ui=ui,
                title="Update",
                description="This app does not expose update controls.",
                detail_text="Add an update provider configuration to enable branch selection and verification.",
            )
            return None

        can_manage_updates: bool = self._user_has_level(user, required_app_mutation_level(NodeAppMutationAction.UPDATE))
        current_model: ModWebBasePageModel = model
        selected_branch_id: str = update_info.selected_branch_id
        dry_preview_statuses: tuple[AppUpdateStatus, ...] = self._dry_update_preview_statuses()
        dry_preview_index: int = -1
        dry_preview_status: AppUpdateStatus | None = None
        from nicegui.context import context as nicegui_context

        notify_client = nicegui_context.client

        def _notify(message: str, *, tone: ModWebNotificationType) -> None:
            with notify_client:
                ui.notify(message, type=tone)

        def _set_selected_branch(event: ModWebValueContainer) -> None:
            nonlocal selected_branch_id
            next_branch_id = _value_as_text(event)
            if next_branch_id == selected_branch_id:
                return
            selected_branch_id = next_branch_id
            render_update_section.refresh(current_model)

        async def _refresh_update_model() -> None:
            nonlocal current_model, selected_branch_id
            if refresh_async_runtime_model is None:
                log.info(
                    "Update section refresh skipped: node=%s app=%s reason=no_runtime_refresh",
                    current_model.node_name,
                    current_model.app_name,
                )
                return
            next_model = await refresh_async_runtime_model()
            previous_status = current_model.update_status
            previous_branch_id = (
                current_model.update_info.selected_branch_id if current_model.update_info is not None else None
            )
            previous_selected_branch_id = selected_branch_id
            previous_view_signature = self._update_section_view_signature(current_model)
            current_model = self._merge_update_section_model(current_model, next_model)
            if next_model.update_info is not None and (
                not selected_branch_id or selected_branch_id == previous_branch_id
            ):
                selected_branch_id = next_model.update_info.selected_branch_id
            next_status = current_model.update_status
            log.debug(
                "Update section refreshed: node=%s app=%s state=%s progress=%s branch=%s",
                current_model.node_name,
                current_model.app_name,
                None if next_status is None else next_status.state.value,
                None if next_status is None else next_status.progress_percent,
                None if next_model.update_info is None else next_model.update_info.selected_branch_id,
            )
            if (
                previous_status is not None
                and previous_status.running
                and next_status is not None
                and not next_status.running
            ):
                _notify(
                    next_status.summary,
                    tone="positive" if next_status.state is AppUpdateState.SUCCEEDED else "negative",
                )
            next_view_signature = self._update_section_view_signature(current_model)
            if next_view_signature != previous_view_signature or selected_branch_id != previous_selected_branch_id:
                render_update_section.refresh(current_model)

        async def _apply_selected_branch(*, notify_result: bool) -> bool:
            nonlocal current_model, selected_branch_id
            current_update_info = current_model.update_info
            if current_update_info is None:
                raise RuntimeError(f"{current_model.app_friendly} does not expose update controls.")
            target_branch_id = self._pending_update_target_branch_id(current_update_info, selected_branch_id)
            if target_branch_id is None:
                selected_branch_id = current_update_info.selected_branch_id
                return False
            log.info(
                "Update branch selection requested from mod web: node=%s app=%s branch=%s user=%s",
                current_model.node_name,
                current_model.app_name,
                target_branch_id,
                user.discord_id,
            )
            result = await self._mutate_app(
                model=current_model,
                action=NodeAppMutationAction.SELECT_UPDATE_BRANCH,
                user=user,
                update_branch_id=target_branch_id,
                timeout_seconds=120.0,
            )
            selected_branch_id = target_branch_id
            if notify_result:
                _notify(result.message, tone="positive")
            await _refresh_update_model()
            return True

        async def _run_update_action(action: NodeAppMutationAction) -> None:
            nonlocal dry_preview_status
            dry_preview_status = None
            try:
                await _apply_selected_branch(notify_result=False)
            except Exception as xcp:
                log.warning(
                    "Update action branch preparation failed: node=%s app=%s action=%s error=%s",
                    current_model.node_name,
                    current_model.app_name,
                    action.value,
                    xcp,
                )
                _notify(f"Update action failed: {xcp}", tone="negative")
                return
            log.info(
                "Update action requested from mod web: node=%s app=%s action=%s user=%s",
                current_model.node_name,
                current_model.app_name,
                action.value,
                user.discord_id,
            )
            try:
                result = await self._mutate_app(
                    model=current_model,
                    action=action,
                    user=user,
                    timeout_seconds=30.0,
                )
            except Exception as xcp:
                log.warning(
                    "App update action failed: node=%s app=%s action=%s error=%s",
                    current_model.node_name,
                    current_model.app_name,
                    action.value,
                    xcp,
                )
                _notify(f"Update action failed: {xcp}", tone="negative")
                return
            log.info(
                "Update action accepted by backend: node=%s app=%s action=%s message=%s",
                current_model.node_name,
                current_model.app_name,
                action.value,
                result.message,
            )
            _notify(result.message, tone="positive")
            await _refresh_update_model()

        def _cycle_dry_preview() -> None:
            nonlocal dry_preview_index, dry_preview_status
            dry_preview_index = (dry_preview_index + 1) % len(dry_preview_statuses)
            dry_preview_status = dry_preview_statuses[dry_preview_index]
            render_update_section.refresh(current_model)
            if dry_preview_status.operation_kind is None:
                raise RuntimeError("Dry update preview statuses must declare an operation kind.")
            _notify(
                f"Dry preview: {dry_preview_status.operation_kind.value.title()} {dry_preview_status.state.value.title()}",
                tone="info",
            )

        def _create_update_action_handler(action: NodeAppMutationAction) -> Callable[[], Awaitable[None]]:
            async def _handle_action() -> None:
                await _run_update_action(action)

            return _handle_action

        @ui.refreshable
        def render_update_section(section_model: ModWebBasePageModel) -> None:
            section_update_info = section_model.update_info
            if section_update_info is None:
                return
            section_update_status = (
                dry_preview_status if dry_preview_status is not None else section_model.update_status
            )
            branch_options = {
                branch.branch_id: f"{branch.label} ({branch.branch_id})" for branch in section_update_info.branches
            }
            resolved_selected_branch_id = self._resolve_update_target_branch_id(section_update_info, selected_branch_id)
            view_state = self._update_section_view_state(
                model=section_model,
                update_info=section_update_info,
                status=section_update_status,
                selected_branch_id=selected_branch_id,
                can_manage_updates=can_manage_updates,
                dry_preview_active=dry_preview_status is not None,
            )

            with ui.card().classes(self._flat_tab_card_classes()):
                with ui.column().classes(self._tab_section_body_classes()):
                    self._render_flat_tab_header(
                        ui=ui,
                        title="Update",
                        description="Choose a target, then update or verify while the app is stopped.",
                        secondary_description=(
                            "Branch changes apply when you run an action."
                            if can_manage_updates
                            else "Sudo access is required for update actions."
                        ),
                    )
                    with ui.element("div").classes("grid grid-cols-1 xl:grid-cols-12 gap-3 w-full"):
                        self._render_update_controls_card(
                            ui=ui,
                            view_state=view_state,
                            branch_options=branch_options,
                            selected_branch_id=resolved_selected_branch_id,
                            on_branch_change=_set_selected_branch,
                            on_update=_create_update_action_handler(NodeAppMutationAction.UPDATE),
                            on_verify=_create_update_action_handler(NodeAppMutationAction.VERIFY),
                            on_refresh=_refresh_update_model if refresh_async_runtime_model is not None else None,
                            on_dry_preview=_cycle_dry_preview if config.INDEV else None,
                        )
                        self._render_update_status_card(ui=ui, view_state=view_state)
                        self._render_update_installed_card(ui=ui, view_state=view_state)
                        self._render_update_log(ui=ui, view_state=view_state)

        render_update_section(current_model)

        def apply_update_model(next_model: ModWebBasePageModel) -> None:
            nonlocal current_model, selected_branch_id
            previous_branch_id = (
                current_model.update_info.selected_branch_id if current_model.update_info is not None else None
            )
            previous_selected_branch_id = selected_branch_id
            previous_view_signature = self._update_section_view_signature(current_model)
            current_model = self._merge_update_section_model(current_model, next_model)
            if current_model.update_info is not None and (
                not selected_branch_id or selected_branch_id == previous_branch_id
            ):
                selected_branch_id = current_model.update_info.selected_branch_id
            next_view_signature = self._update_section_view_signature(current_model)
            if next_view_signature != previous_view_signature or selected_branch_id != previous_selected_branch_id:
                render_update_section.refresh(current_model)

        return apply_update_model

    @staticmethod
    def _update_unix_ms_now() -> int:
        return int(time.time() * 1000)

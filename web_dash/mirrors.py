"""App-agnostic update-mirror management UI."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, cast

from _async_utils import run_blocking
from computercraft_mirror import (
    COMPUTERCRAFT_MIRROR_STARTUP_DISPATCHER_PATH,
    COMPUTERCRAFT_MIRROR_STATE_ROOT,
)
from mirror_models import (
    GitMirrorSource,
    GitReferenceOption,
    GitRepositoryInspection,
    MirrorError,
    MirrorProject,
    MirrorSyncState,
    MirrorTrackingMode,
)
from mirror_service import MirrorService

from .nicegui_protocols import ModWebUi, _value_as_text
from .runtime_imports import ModWebUser, Power_Level, config
from .service_base import ModWebServiceSupport
from .ui_helpers import copy_text_to_clipboard

if TYPE_CHECKING:
    from nicegui.elements.upload_files import FileUpload
    from nicegui.events import MultiUploadEventArguments


_MIRROR_TRACKING_MODE_OPTIONS: Final[dict[str, str]] = {
    MirrorTrackingMode.BRANCH.value: "Track branch",
    MirrorTrackingMode.PINNED_COMMIT.value: "Pin exact commit",
}


class _MirrorReferenceSelector(Protocol):
    def set_options(self, options: dict[str, str], *, value: str | None = None) -> None: ...


class ModWebMirrorsMixin(ModWebServiceSupport):
    """Render and operate the dashboard's public update mirrors."""

    _MIRROR_PAGE_PATH = "/mod-web/mirrors"

    def _mirror_service(self) -> MirrorService:
        mirrors = self._mirrors
        if mirrors is None:
            raise RuntimeError("Update mirrors are hosted exclusively by the Portal profile.")
        return mirrors

    @staticmethod
    def _git_reference_option_labels(
        *,
        options: tuple[GitReferenceOption, ...],
        tracking_mode: MirrorTrackingMode,
        selected_ref: str | None = None,
    ) -> dict[str, str]:
        labels = {option.ref: option.label for option in options}
        if selected_ref and selected_ref not in labels:
            selected_label = selected_ref if tracking_mode is MirrorTrackingMode.BRANCH else selected_ref[:12]
            labels[selected_ref] = f"{selected_label} · Selected reference"
        return labels

    @staticmethod
    def _render_publish_root_help(*, ui: ModWebUi) -> None:
        ui.label(
            "Publish root is the folder inside the repository or ZIP that becomes the mirror's top-level directory. "
            "Leave it blank to publish everything; for example, 'release' makes release/startup.lua available as startup.lua."
        ).classes("mod-subtitle text-xs md:col-span-2")

    @staticmethod
    def _mirror_installer_url() -> str:
        return f"{config.MOD_WEB_SERVER.public_base_url.rstrip('/')}/mirror/v1/installer.lua"

    @classmethod
    def _computercraft_install_command(cls, project: MirrorProject, *, enable_startup: bool = True) -> str:
        installation_root = f"/{project.project_id}"
        command = (
            f"wget run {cls._mirror_installer_url()} {project.project_id} "
            f"{cls._mirror_public_base_url(project)} {installation_root}"
        )
        return f"{command} --enable-startup" if enable_startup else command

    @staticmethod
    def _computercraft_startup_snippet(*, project: MirrorProject) -> str:
        return (
            f'pcall(function() shell.run("{COMPUTERCRAFT_MIRROR_STARTUP_DISPATCHER_PATH}") end)\n'
            f'shell.run("/{project.project_id}/startup.lua")'
        )

    async def _render_mirrors_page(self, *, ui: ModWebUi, user: ModWebUser) -> None:
        self._apply_theme_for_user(ui=ui, user=user)
        self._render_skip_link(ui=ui)
        can_manage_all = self._user_has_level(user, Power_Level.admin)
        mirrors = self._mirror_service()
        projects = mirrors.list_projects(actor_user_id=user.discord_id, can_manage_all=can_manage_all)

        with (
            ui.column()
            .classes("mod-page w-full gap-6 px-4 py-8 md:px-8")
            .props("id=mod-main-content role=main tabindex=-1")
        ):
            self._render_user_header(ui=ui, user=user)
            with ui.row().classes("w-full items-start justify-between gap-4 flex-wrap"):
                with ui.column().classes("gap-1"):
                    ui.label("Update Mirrors").classes("text-2xl font-semibold")
                    ui.label(
                        "Validated snapshots for ComputerCraft. Git mirrors track master by default; "
                        "uploaded ZIP archives are immutable."
                    ).classes("mod-subtitle text-sm max-w-3xl")
                with ui.row().classes("gap-2 flex-wrap"):
                    ui.button(
                        "Link Git repository",
                        icon="link",
                        on_click=lambda: self._open_git_mirror_dialog(ui=ui, user=user),
                    ).classes("mod-list-button")
                    ui.button(
                        "Upload ZIP",
                        icon="upload_file",
                        on_click=lambda: self._open_upload_mirror_dialog(ui=ui, user=user),
                    ).classes("mod-list-button secondary")

            if not projects:
                with ui.card().classes("mod-card mod-card-empty w-full"):
                    ui.label("No update mirrors yet.").classes("font-medium")
                    ui.label(
                        "Link a public GitHub/GitLab repository or upload a ZIP archive to publish your first snapshot."
                    ).classes("mod-subtitle text-sm")
                return

            with ui.column().classes("w-full gap-4"):
                for project in projects:
                    self._render_mirror_project_card(
                        ui=ui,
                        user=user,
                        project=project,
                        can_manage_all=can_manage_all,
                    )

    def _render_mirror_project_card(
        self,
        *,
        ui: ModWebUi,
        user: ModWebUser,
        project: MirrorProject,
        can_manage_all: bool,
    ) -> None:
        base_url = self._mirror_public_base_url(project)
        with ui.card().classes("mod-card w-full"):
            with ui.row().classes("w-full items-start justify-between gap-3 flex-wrap"):
                with ui.column().classes("gap-1 min-w-0"):
                    ui.label(project.display_name).classes("text-lg font-medium break-words")
                    ui.label(project.source_label).classes("mod-subtitle text-sm break-all")
                    ui.label(project.tracking_label).classes("mod-subtitle text-sm")
                self._badge(
                    ui=ui,
                    text=project.sync_state.value.replace("_", " ").title(),
                    tone=self._mirror_sync_state_tone(project.sync_state),
                )
            with ui.row().classes("w-full gap-x-6 gap-y-2 flex-wrap text-sm"):
                ui.label(f"ID: {project.project_id}").classes("mod-subtitle")
                if project.published_revision is not None:
                    ui.label(f"Revision: {project.published_revision[:12]}").classes("mod-subtitle")
                if project.published_at is not None:
                    ui.label(f"Published: {project.published_at}").classes("mod-subtitle")
                if project.last_checked_at is not None:
                    ui.label(f"Last source check: {project.last_checked_at}").classes("mod-subtitle")
                if project.next_check_at is not None:
                    ui.label(f"Next daily check: {project.next_check_at}").classes("mod-subtitle")
                if project.publish_root:
                    ui.label(f"Publish root: {project.publish_root}").classes("mod-subtitle")
            if project.status_detail is not None:
                ui.label(project.status_detail).classes("mod-subtitle text-sm")
            with ui.row().classes("w-full gap-2 flex-wrap"):
                ui.button(
                    "Sync now",
                    icon="sync",
                    on_click=lambda project_id=project.project_id: self._refresh_mirror_action(
                        ui=ui,
                        user=user,
                        project_id=project_id,
                        can_manage_all=can_manage_all,
                    ),
                ).classes("mod-list-button")
                if (
                    isinstance(project.source, GitMirrorSource)
                    and project.source.tracking_mode is MirrorTrackingMode.BRANCH
                ):
                    ui.button(
                        "Pin current",
                        icon="push_pin",
                        on_click=lambda project_id=project.project_id: self._pin_mirror_action(
                            ui=ui,
                            user=user,
                            project_id=project_id,
                            can_manage_all=can_manage_all,
                        ),
                    ).classes("mod-list-button secondary")
                elif isinstance(project.source, GitMirrorSource):
                    ui.button(
                        "Track master",
                        icon="alt_route",
                        on_click=lambda project_id=project.project_id: self._track_master_action(
                            ui=ui,
                            user=user,
                            project_id=project_id,
                            can_manage_all=can_manage_all,
                        ),
                    ).classes("mod-list-button secondary")
                if project.sync_state is not MirrorSyncState.DISABLED:
                    ui.button(
                        "Disable",
                        icon="block",
                        on_click=lambda project_id=project.project_id: self._disable_mirror_action(
                            ui=ui,
                            user=user,
                            project_id=project_id,
                            can_manage_all=can_manage_all,
                        ),
                    ).classes("mod-list-button secondary")
                ui.button(
                    "Copy base URL",
                    icon="content_copy",
                    on_click=lambda url=base_url: copy_text_to_clipboard(
                        ui=ui,
                        text=url,
                        empty_message="Mirror URL is unavailable.",
                    ),
                ).classes("mod-list-button secondary")
                if project.is_snapshot_available:
                    ui.button(
                        "ComputerCraft setup",
                        icon="terminal",
                        on_click=lambda project=project: self._open_computercraft_setup_dialog(ui=ui, project=project),
                    ).classes("mod-list-button secondary")
                    ui.link("Manifest", f"{base_url}/manifest.json", new_tab=True).classes("mod-list-button secondary")

    def _open_computercraft_setup_dialog(self, *, ui: ModWebUi, project: MirrorProject) -> None:
        has_computercraft_startup = (
            self._mirror_service().file_path(project_id=project.project_id, relative_path="startup.lua") is not None
        )
        automatic_install_command = self._computercraft_install_command(project)
        manual_install_command = self._computercraft_install_command(project, enable_startup=False)
        startup_snippet = self._computercraft_startup_snippet(project=project)
        with ui.dialog() as dialog:
            with ui.card().classes("mod-card mod-dialog-card mod-app-details-dialog-card"):
                with ui.column().classes("w-full gap-4 p-5 mod-app-details-layout"):
                    ui.label("Set up ComputerCraft").classes("text-xl font-black mod-title-small")
                    if has_computercraft_startup:
                        ui.label(
                            "Run this once in the ComputerCraft terminal. The computer must be allowed to download from "
                            "the web. It installs this program, checks for updates whenever the computer starts, then "
                            "starts it—even if Yukibot cannot be reached."
                        ).classes("mod-subtitle text-sm")
                        with ui.column().classes("w-full gap-2 mod-app-details-subsection"):
                            ui.label("Recommended: automatic updates and start-up").classes("mod-stat-label")
                            ui.label(automatic_install_command).classes("mod-chat-inline-code break-all")
                            ui.button(
                                "Copy recommended command",
                                icon="content_copy",
                                on_click=lambda: copy_text_to_clipboard(
                                    ui=ui,
                                    text=automatic_install_command,
                                    empty_message="The recommended ComputerCraft command is unavailable.",
                                ),
                            ).classes("mod-list-button secondary self-start")
                        with ui.column().classes("w-full gap-2 mod-app-details-subsection"):
                            ui.label("Advanced: existing startup.lua").classes("mod-stat-label")
                            ui.label(
                                "Already have a startup.lua? Add this at the beginning of it. It checks every program "
                                "installed by Yukibot for updates, then starts this program. If Yukibot is unavailable, "
                                "it starts the version already on the computer."
                            ).classes("mod-subtitle text-sm")
                            ui.label(startup_snippet).classes("mod-chat-inline-code break-all")
                            ui.button(
                                "Copy startup snippet",
                                icon="content_copy",
                                on_click=lambda: copy_text_to_clipboard(
                                    ui=ui,
                                    text=startup_snippet,
                                    empty_message="The Yukibot startup snippet is unavailable.",
                                ),
                            ).classes("mod-list-button secondary self-start")
                    else:
                        ui.label(
                            "This program cannot start automatically because its published folder has no startup.lua. "
                            "Install it manually, then start the appropriate program from your own startup.lua."
                        ).classes("mod-subtitle text-sm")
                    with ui.column().classes("w-full gap-2 mod-app-details-subsection"):
                        ui.label("Advanced: install without automatic updates").classes("mod-stat-label")
                        ui.label(
                            "Use this if you prefer to update manually. The final "
                            f"/{project.project_id} part is where the files are installed; only change it if you need a "
                            "different folder. Automatic setup needs a "
                            "startup.lua in the published folder."
                        ).classes("mod-subtitle text-sm")
                        ui.label(manual_install_command).classes("mod-chat-inline-code break-all")
                        ui.button(
                            "Copy manual command",
                            icon="content_copy",
                            on_click=lambda: copy_text_to_clipboard(
                                ui=ui,
                                text=manual_install_command,
                                empty_message="The manual ComputerCraft command is unavailable.",
                            ),
                        ).classes("mod-list-button secondary self-start")
                    with ui.column().classes("w-full gap-2 mod-app-details-subsection"):
                        ui.label("Repository layout for automatic start-up").classes("mod-stat-label")
                        ui.label(
                            "Keep startup.lua directly inside the folder Yukibot publishes. This is your repository root "
                            "unless you chose a Publish root when creating the mirror. Other files and folders can be "
                            "organised however your program needs."
                        ).classes("mod-subtitle text-sm")
                        ui.label("startup.lua\nexample/\n  example.lua").classes("mod-chat-inline-code break-all")
                    ui.label(
                        "Yukibot only changes files it installed itself. Its update information is stored under "
                        f"{COMPUTERCRAFT_MIRROR_STATE_ROOT}; you normally do not need to touch it."
                    ).classes("mod-subtitle text-xs")
                    with ui.row().classes("w-full justify-end"):
                        ui.button("Close", on_click=dialog.close).classes("mod-list-button secondary")
        dialog.open()

    def _open_git_mirror_dialog(self, *, ui: ModWebUi, user: ModWebUser) -> None:
        mirrors = self._mirror_service()
        inspection: GitRepositoryInspection | None = None
        reference_options: dict[MirrorTrackingMode, dict[str, str]] = {}
        with ui.dialog() as dialog:
            with ui.card().classes("mod-card mod-dialog-card mod-app-details-dialog-card"):

                @ui.refreshable
                def render_dialog_content() -> None:
                    nonlocal inspection
                    with ui.column().classes("w-full gap-4 p-5 mod-app-details-layout"):
                        if inspection is None:
                            with ui.column().classes("gap-1"):
                                ui.label("Link Git repository").classes("text-xl font-black mod-title-small")
                                ui.label(
                                    "Paste a public GitHub or GitLab project, branch, or commit URL. "
                                    "We will fetch its details before creating the mirror."
                                ).classes("mod-subtitle text-sm")
                            repository_url_input = (
                                ui.input("Repository URL", placeholder="https://github.com/owner/repository")
                                .props("filled square dense clearable hide-bottom-space color=accent")
                                .classes("w-full mod-app-details-field mod-config-input")
                            )

                            async def inspect_repository() -> None:
                                nonlocal inspection
                                try:
                                    candidate_inspection = await run_blocking(
                                        mirrors.inspect_git_repository_url,
                                        _value_as_text(repository_url_input),
                                    )
                                    source_options = await run_blocking(
                                        mirrors.list_git_reference_options,
                                        host=candidate_inspection.source.host,
                                        repository=candidate_inspection.source.repository,
                                        tracking_mode=candidate_inspection.source.tracking_mode,
                                    )
                                except Exception as xcp:
                                    ui.notify(f"Could not inspect repository: {xcp}", type="negative", multi_line=True)
                                    return
                                inspection = candidate_inspection
                                reference_options.clear()
                                reference_options[candidate_inspection.source.tracking_mode] = (
                                    self._git_reference_option_labels(
                                        options=source_options,
                                        tracking_mode=candidate_inspection.source.tracking_mode,
                                        selected_ref=candidate_inspection.source.ref,
                                    )
                                )
                                render_dialog_content.refresh()

                            with ui.row().classes("w-full justify-end gap-2"):
                                ui.button("Cancel", on_click=dialog.close).classes("mod-list-button secondary")
                                ui.button(
                                    "Fetch repository", icon="travel_explore", on_click=inspect_repository
                                ).classes("mod-list-button")
                            return

                        current_inspection = inspection
                        detected_source = current_inspection.source
                        selected_reference_options = reference_options.get(detected_source.tracking_mode)
                        if selected_reference_options is None:
                            raise RuntimeError("Git reference options were not loaded.")

                        def preferred_reference_for(tracking_mode: MirrorTrackingMode) -> str | None:
                            if tracking_mode is detected_source.tracking_mode:
                                return detected_source.ref
                            if tracking_mode is MirrorTrackingMode.BRANCH:
                                return current_inspection.default_branch
                            return None

                        with ui.row().classes("w-full items-start justify-between gap-3 flex-wrap"):
                            with ui.column().classes("gap-1 min-w-0"):
                                ui.label("Configure mirror").classes("text-xl font-black mod-title-small")
                                ui.label(
                                    f"{detected_source.host.value.title()} · {detected_source.repository}"
                                ).classes("mod-subtitle text-sm break-all")
                            detected_label = (
                                "Commit link detected"
                                if detected_source.tracking_mode is MirrorTrackingMode.PINNED_COMMIT
                                else f"Default branch: {detected_source.ref}"
                            )
                            self._badge(ui=ui, text=detected_label, tone="black")
                        with ui.element("div").classes("w-full grid grid-cols-1 md:grid-cols-2 gap-3"):
                            project_id_input = (
                                ui.input("Mirror ID", value=current_inspection.suggested_project_id)
                                .props("filled square dense clearable hide-bottom-space color=accent")
                                .classes("w-full mod-app-details-field mod-config-input")
                            )
                            display_name_input = (
                                ui.input("Display name", value=current_inspection.display_name)
                                .props("filled square dense clearable hide-bottom-space color=accent")
                                .classes("w-full mod-app-details-field mod-config-input")
                            )
                            tracking_mode_input = (
                                ui.select(
                                    _MIRROR_TRACKING_MODE_OPTIONS,
                                    value=detected_source.tracking_mode.value,
                                    label="Source mode",
                                )
                                .props(
                                    "filled square dense hide-bottom-space color=accent options-dark "
                                    "popup-content-class=mod-setting-menu"
                                )
                                .classes("w-full mod-app-details-field mod-config-select")
                            )
                            ref_input = (
                                ui.select(
                                    selected_reference_options,
                                    value=detected_source.ref,
                                    label="Branch or commit",
                                )
                                .props(
                                    "filled square dense hide-bottom-space color=accent options-dark "
                                    "popup-content-class=mod-setting-menu"
                                )
                                .classes("w-full mod-app-details-field mod-config-select")
                            )
                            publish_root_input = (
                                ui.input("Publish root (optional)", placeholder="release")
                                .props("filled square dense clearable hide-bottom-space color=accent")
                                .classes("w-full mod-app-details-field mod-config-input md:col-span-2")
                            )
                            self._render_publish_root_help(ui=ui)

                        active_tracking_mode = detected_source.tracking_mode

                        async def source_mode_changed(_: object) -> None:
                            nonlocal active_tracking_mode
                            try:
                                requested_tracking_mode = MirrorTrackingMode(_value_as_text(tracking_mode_input))
                            except ValueError:
                                ui.notify("Choose a valid source mode.", type="negative")
                                return
                            if requested_tracking_mode is active_tracking_mode:
                                return
                            next_options = reference_options.get(requested_tracking_mode)
                            if next_options is None:
                                try:
                                    provider_options = await run_blocking(
                                        mirrors.list_git_reference_options,
                                        host=detected_source.host,
                                        repository=detected_source.repository,
                                        tracking_mode=requested_tracking_mode,
                                    )
                                except Exception as xcp:
                                    tracking_mode_input.set_value(active_tracking_mode.value)
                                    ui.notify(f"Could not load Git references: {xcp}", type="negative", multi_line=True)
                                    return
                                preferred_ref = preferred_reference_for(requested_tracking_mode)
                                next_options = self._git_reference_option_labels(
                                    options=provider_options,
                                    tracking_mode=requested_tracking_mode,
                                    selected_ref=preferred_ref,
                                )
                                reference_options[requested_tracking_mode] = next_options
                            if MirrorTrackingMode(_value_as_text(tracking_mode_input)) is not requested_tracking_mode:
                                return
                            next_ref = next(iter(next_options))
                            preferred_ref = preferred_reference_for(requested_tracking_mode)
                            if preferred_ref in next_options:
                                next_ref = preferred_ref
                            reference_selector = cast(_MirrorReferenceSelector, cast(object, ref_input))
                            reference_selector.set_options(next_options, value=next_ref)
                            active_tracking_mode = requested_tracking_mode

                        tracking_mode_input.on("update:model-value", source_mode_changed)

                        def change_repository() -> None:
                            nonlocal inspection
                            inspection = None
                            render_dialog_content.refresh()

                        async def create_git_mirror() -> None:
                            try:
                                tracking_mode = MirrorTrackingMode(_value_as_text(tracking_mode_input))
                                source = GitMirrorSource(
                                    host=detected_source.host,
                                    repository=detected_source.repository,
                                    tracking_mode=tracking_mode,
                                    ref=_value_as_text(ref_input),
                                )
                                project = await run_blocking(
                                    mirrors.create_git_project_from_source,
                                    project_id=_value_as_text(project_id_input),
                                    display_name=_value_as_text(display_name_input),
                                    owner_user_id=user.discord_id,
                                    source=source,
                                    publish_root=_value_as_text(publish_root_input),
                                )
                                await run_blocking(
                                    mirrors.refresh_project,
                                    project_id=project.project_id,
                                    actor_user_id=user.discord_id,
                                    can_manage_all=self._user_has_level(user, Power_Level.admin),
                                )
                            except Exception as xcp:
                                ui.notify(f"Could not create mirror: {xcp}", type="negative", multi_line=True)
                                return
                            dialog.close()
                            ui.notify("Mirror published.", type="positive")
                            self._guarded_reload(ui=ui)

                        with ui.row().classes("w-full justify-between gap-2 flex-wrap"):
                            ui.button("Change URL", icon="edit", on_click=change_repository).classes(
                                "mod-list-button secondary"
                            )
                            with ui.row().classes("gap-2"):
                                ui.button("Cancel", on_click=dialog.close).classes("mod-list-button secondary")
                                ui.button("Create and sync", icon="sync", on_click=create_git_mirror).classes(
                                    "mod-list-button"
                                )

                render_dialog_content()
        dialog.open()

    def _open_upload_mirror_dialog(self, *, ui: ModWebUi, user: ModWebUser) -> None:
        mirrors = self._mirror_service()
        with ui.dialog() as dialog:
            with ui.card().classes("mod-card mod-dialog-card mod-app-details-dialog-card"):
                with ui.column().classes("w-full gap-4 p-5 mod-app-details-layout"):
                    with ui.column().classes("gap-1"):
                        ui.label("Upload ZIP mirror").classes("text-xl font-black mod-title-small")
                        ui.label(
                            "The ZIP is stored as an immutable source, checked server-side, then published as a snapshot."
                        ).classes("mod-subtitle text-sm")
                    with ui.element("div").classes("w-full grid grid-cols-1 md:grid-cols-2 gap-3"):
                        project_id_input = (
                            ui.input("Mirror ID", placeholder="my-computercraft-program")
                            .props("filled square dense clearable hide-bottom-space color=accent")
                            .classes("w-full mod-app-details-field mod-config-input")
                        )
                        display_name_input = (
                            ui.input("Display name", placeholder="My ComputerCraft program")
                            .props("filled square dense clearable hide-bottom-space color=accent")
                            .classes("w-full mod-app-details-field mod-config-input")
                        )
                        publish_root_input = (
                            ui.input("Publish root (optional)", placeholder="release")
                            .props("filled square dense clearable hide-bottom-space color=accent")
                            .classes("w-full mod-app-details-field mod-config-input md:col-span-2")
                        )
                        self._render_publish_root_help(ui=ui)

                    async def upload_archive(event: "MultiUploadEventArguments") -> None:
                        files = tuple(event.files)
                        if len(files) != 1:
                            ui.notify("Choose exactly one ZIP archive.", type="warning")
                            return
                        archive_path: Path | None = None
                        try:
                            archive_path = await self._persist_mirror_archive(files[0])
                            project = await run_blocking(
                                mirrors.create_upload_project,
                                project_id=_value_as_text(project_id_input),
                                display_name=_value_as_text(display_name_input),
                                owner_user_id=user.discord_id,
                                archive_path=archive_path,
                                publish_root=_value_as_text(publish_root_input),
                            )
                            await run_blocking(
                                mirrors.refresh_project,
                                project_id=project.project_id,
                                actor_user_id=user.discord_id,
                                can_manage_all=self._user_has_level(user, Power_Level.admin),
                            )
                        except Exception as xcp:
                            ui.notify(f"Could not publish upload: {xcp}", type="negative", multi_line=True)
                            return
                        finally:
                            if archive_path is not None:
                                archive_path.unlink(missing_ok=True)
                        dialog.close()
                        ui.notify("Mirror published.", type="positive")
                        self._guarded_reload(ui=ui)

                    ui.upload(
                        label="Choose ZIP archive",
                        auto_upload=True,
                        multiple=True,
                        max_files=1,
                        max_file_size=32 * 1024 * 1024,
                        on_multi_upload=upload_archive,
                    ).props("accept=.zip").classes("w-full mod-app-details-section")
                    with ui.row().classes("w-full justify-end"):
                        ui.button("Cancel", on_click=dialog.close).classes("mod-list-button secondary")
        dialog.open()

    async def _refresh_mirror_action(
        self,
        *,
        ui: ModWebUi,
        user: ModWebUser,
        project_id: str,
        can_manage_all: bool,
    ) -> None:
        mirrors = self._mirror_service()
        try:
            await run_blocking(
                mirrors.refresh_project,
                project_id=project_id,
                actor_user_id=user.discord_id,
                can_manage_all=can_manage_all,
            )
        except Exception as xcp:
            ui.notify(f"Mirror sync failed: {xcp}", type="negative", multi_line=True)
            self._guarded_reload(ui=ui)
            return
        ui.notify("Mirror published.", type="positive")
        self._guarded_reload(ui=ui)

    async def _pin_mirror_action(
        self,
        *,
        ui: ModWebUi,
        user: ModWebUser,
        project_id: str,
        can_manage_all: bool,
    ) -> None:
        mirrors = self._mirror_service()
        try:
            await run_blocking(
                mirrors.pin_current_revision,
                project_id=project_id,
                actor_user_id=user.discord_id,
                can_manage_all=can_manage_all,
            )
        except Exception as xcp:
            ui.notify(f"Could not pin mirror: {xcp}", type="negative", multi_line=True)
            return
        ui.notify("Mirror revision pinned.", type="positive")
        self._guarded_reload(ui=ui)

    async def _track_master_action(
        self,
        *,
        ui: ModWebUi,
        user: ModWebUser,
        project_id: str,
        can_manage_all: bool,
    ) -> None:
        mirrors = self._mirror_service()
        try:
            await run_blocking(
                mirrors.track_master,
                project_id=project_id,
                actor_user_id=user.discord_id,
                can_manage_all=can_manage_all,
            )
        except Exception as xcp:
            ui.notify(f"Could not update mirror tracking: {xcp}", type="negative", multi_line=True)
            return
        ui.notify("Mirror now tracks master. Sync to publish it.", type="positive")
        self._guarded_reload(ui=ui)

    async def _disable_mirror_action(
        self,
        *,
        ui: ModWebUi,
        user: ModWebUser,
        project_id: str,
        can_manage_all: bool,
    ) -> None:
        mirrors = self._mirror_service()
        try:
            await run_blocking(
                mirrors.disable_project,
                project_id=project_id,
                actor_user_id=user.discord_id,
                can_manage_all=can_manage_all,
            )
        except Exception as xcp:
            ui.notify(f"Could not disable mirror: {xcp}", type="negative", multi_line=True)
            return
        ui.notify("Mirror disabled.", type="positive")
        self._guarded_reload(ui=ui)

    @staticmethod
    async def _persist_mirror_archive(upload_file: "FileUpload") -> Path:
        if Path(upload_file.name).suffix.casefold() != ".zip":
            raise MirrorError("Mirror uploads must be ZIP archives.")
        with tempfile.NamedTemporaryFile(prefix="yukibot-mirror-upload-", suffix=".zip", delete=False) as handle:
            path = Path(handle.name)
        try:
            await upload_file.save(path)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return path

    @classmethod
    def _mirror_public_base_url(cls, project: MirrorProject) -> str:
        return f"{config.MOD_WEB_SERVER.public_base_url.rstrip('/')}/mirror/v1/projects/{project.project_id}"

    @staticmethod
    def _mirror_sync_state_tone(state: MirrorSyncState) -> str:
        if state is MirrorSyncState.PUBLISHED:
            return "black"
        if state is MirrorSyncState.PUBLISHING:
            return "purple"
        if state is MirrorSyncState.FAILED:
            return "red"
        return "grey"

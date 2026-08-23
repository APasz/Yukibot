"""Sudo-only, recipe-driven app installation page."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from functools import partial
from threading import RLock
from typing import TYPE_CHECKING, Literal, cast
from uuid import uuid4

from _manager import AppInstallInput
from node_api_app_installer import (
    NodeAppInstallCatalog,
    NodeAppInstallInputKind,
    NodeAppInstallRecipe,
    NodeAppInstallRequest,
    NodeAppInstallStatus,
)
from node_auth import NodeApiScope

from .nicegui_protocols import ModWebUi
from .runtime_imports import Button, Card, Input, Label, ModWebUser, Select, Textarea, Timer, asyncio
from .service_base import ModWebServiceSupport
from .types import ModWebNodeLink
from .ui_helpers import ModWebUiHelpersMixin

if TYPE_CHECKING:
    from nicegui.elements.link import Link
    from nicegui.events import ValueChangeEventArguments

_INSTALL_STATUS_REFRESH_SECONDS = 1.0
_INSTALL_FIELD_PROPS = "filled square dense clearable hide-bottom-space color=accent"
_INSTALL_SELECT_PROPS = "filled square dense hide-bottom-space color=accent options-dark popup-content-class=mod-setting-menu"
_AppInstallerTextField = Literal["instance_key", "friendly_name", "subfolder", "port_text", "steam_branch_id"]


class _AppInstallerWizardStep(enum.StrEnum):
    NODE = "node"
    APP = "app"
    DETAILS = "details"

    @property
    def number(self) -> int:
        if self is _AppInstallerWizardStep.NODE:
            return 1
        if self is _AppInstallerWizardStep.APP:
            return 2
        if self is _AppInstallerWizardStep.DETAILS:
            return 3
        raise ValueError(f"Unknown app installer step: {self}")

    @property
    def label(self) -> str:
        if self is _AppInstallerWizardStep.NODE:
            return "Node"
        if self is _AppInstallerWizardStep.APP:
            return "App"
        if self is _AppInstallerWizardStep.DETAILS:
            return "Details"
        raise ValueError(f"Unknown app installer step: {self}")


@dataclass(slots=True)
class _AppInstallerPageState:
    node_name: str
    step: _AppInstallerWizardStep = _AppInstallerWizardStep.NODE
    catalog: NodeAppInstallCatalog | None = None
    recipe_scope: str | None = None
    instance_key: str = ""
    friendly_name: str = ""
    subfolder: str = ""
    port_text: str = ""
    steam_branch_id: str = ""
    inputs: dict[AppInstallInput, str] = field(default_factory=dict)
    catalog_error: str | None = None
    job_id: str | None = None
    status: NodeAppInstallStatus | None = None
    status_error: str | None = None
    status_polling: bool = False
    install_starting: bool = False

    def apply_catalog(self, catalog: NodeAppInstallCatalog) -> None:
        self.catalog = catalog
        self.catalog_error = None
        selected_recipe = _recipe_from_catalog(catalog=catalog, scope=self.recipe_scope)
        if selected_recipe is None and catalog.recipes:
            selected_recipe = catalog.recipes[0]
        self.apply_recipe(selected_recipe)

    def apply_recipe(self, recipe: NodeAppInstallRecipe | None) -> None:
        self.recipe_scope = None if recipe is None else recipe.scope
        if recipe is None:
            self.instance_key = ""
            self.friendly_name = ""
            self.subfolder = ""
            self.port_text = ""
            self.steam_branch_id = ""
            self.inputs.clear()
            return
        self.instance_key = "server"
        self.friendly_name = f"{recipe.label} Server"
        self.subfolder = f"{recipe.scope}-server"
        self.port_text = "" if recipe.default_port is None else str(recipe.default_port)
        self.steam_branch_id = recipe.default_branch_id
        self.inputs = {AppInstallInput(install_input.key): "" for install_input in recipe.fields}

    def clear_job(self) -> None:
        self.job_id = None
        self.status = None
        self.status_error = None
        self.status_polling = False
        self.install_starting = False


@dataclass(slots=True)
class _AppInstallerStatusControls:
    card: Card
    state_label: Label
    summary_label: Label
    progress_label: Label
    detail_label: Label
    app_link: Link
    log_textarea: Textarea
    app_path: str = "#"


@dataclass(frozen=True, slots=True)
class _AppInstallerPageLease:
    owner_token: str
    node_name: str
    job_id: str | None = None


class _AppInstallerPageLock:
    """Keep one dashboard install workflow active at a time."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._lease: _AppInstallerPageLease | None = None

    def current(self) -> _AppInstallerPageLease | None:
        with self._lock:
            return self._lease

    def acquire(self, *, owner_token: str, node_name: str) -> bool:
        with self._lock:
            lease = self._lease
            if lease is not None:
                return lease.owner_token == owner_token
            self._lease = _AppInstallerPageLease(owner_token=owner_token, node_name=node_name)
            return True

    def record_job(self, *, owner_token: str, node_name: str, job_id: str) -> None:
        with self._lock:
            lease = self._lease
            if lease is None or lease.owner_token != owner_token:
                raise RuntimeError("App installer page lock was released before the job was recorded.")
            self._lease = _AppInstallerPageLease(
                owner_token=owner_token,
                node_name=node_name,
                job_id=job_id,
            )

    def release(self, *, owner_token: str) -> bool:
        with self._lock:
            lease = self._lease
            if lease is None or lease.owner_token != owner_token:
                return False
            self._lease = None
            return True

    def release_completed_job(self, *, job_id: str) -> bool:
        with self._lock:
            lease = self._lease
            if lease is None or lease.job_id != job_id:
                return False
            self._lease = None
            return True


def _recipe_from_catalog(*, catalog: NodeAppInstallCatalog | None, scope: str | None) -> NodeAppInstallRecipe | None:
    if catalog is None or scope is None:
        return None
    scope_key = scope.casefold()
    return next((recipe for recipe in catalog.recipes if recipe.scope.casefold() == scope_key), None)


def _optional_port(raw_port: str) -> int | None:
    text = raw_port.strip()
    if not text:
        return None
    if not text.isascii() or not text.isdecimal():
        raise ValueError("Port must use digits.")
    return int(text)


class ModWebAppInstallerMixin(ModWebServiceSupport):
    """Render and broker recipe-backed installs across dashboard nodes."""

    _app_installer_page_lock: _AppInstallerPageLock = cast(_AppInstallerPageLock, cast(object, None))

    async def _render_app_installer_page(self, *, ui: ModWebUi, user: ModWebUser) -> None:
        self._apply_theme_for_user(ui=ui, user=user)
        ModWebUiHelpersMixin._render_skip_link(ui=ui)
        nodes = tuple(node for node in self._node_links() if not self._node_is_portal(node))

        with ui.column().classes("w-full gap-6 px-4 py-8 md:px-8"):
            with ui.column().classes("mod-page w-full gap-6").props("id=mod-main-content role=main tabindex=-1"):
                self._render_user_header(ui=ui, user=user)
                with ui.card().classes("mod-card w-full"):
                    with ui.column().classes("w-full gap-2"):
                        ui.label("App Installer").classes("text-2xl font-black mod-title-small")
                        ui.label("Choose a node, app, and install settings.").classes("mod-subtitle text-sm")

                if not nodes:
                    with ui.card().classes("mod-card w-full"):
                        ui.label("No app nodes are available.").classes("mod-subtitle")
                    return

                await self._render_app_installer_wizard(ui=ui, user=user, nodes=nodes)

    async def _render_app_installer_wizard(
        self,
        *,
        ui: ModWebUi,
        user: ModWebUser,
        nodes: tuple[ModWebNodeLink, ...],
    ) -> None:
        nodes_by_name = {node.node_name.casefold(): node for node in nodes}
        state = _AppInstallerPageState(node_name=nodes[0].node_name)
        page_token = uuid4().hex
        status_controls: _AppInstallerStatusControls | None = None

        def selected_node() -> ModWebNodeLink:
            node = nodes_by_name.get(state.node_name.casefold())
            if node is None:
                raise RuntimeError("Selected node is no longer available.")
            return node

        def install_is_active() -> bool:
            return state.install_starting or (state.status is not None and state.status.running)

        def page_lock_is_held_by_other() -> bool:
            lease = self._app_installer_page_lock.current()
            return lease is not None and lease.owner_token != page_token

        def wizard_is_locked() -> bool:
            return install_is_active() or page_lock_is_held_by_other()

        def reject_when_wizard_locked() -> bool:
            if install_is_active():
                ui.notify("Wait for the install to finish.", type="warning")
                return True
            if page_lock_is_held_by_other():
                ui.notify("Another dashboard session is already starting or running an install.", type="warning")
                return True
            return False

        def disable_when_wizard_locked(*controls: Button | Input | Select) -> None:
            if wizard_is_locked():
                for control in controls:
                    control.disable()

        async def load_catalog() -> None:
            try:
                catalog = await self._app_install_catalog(node=selected_node(), user=user)
            except Exception as xcp:
                state.catalog = None
                state.catalog_error = str(xcp) or type(xcp).__name__
                state.apply_recipe(None)
                return
            state.apply_catalog(catalog)

        def create_status_controls() -> _AppInstallerStatusControls:
            with ui.card().classes("mod-card w-full") as status_card:
                with ui.column().classes("w-full gap-3"):
                    with ui.row().classes("w-full items-center justify-between gap-3 flex-wrap"):
                        ui.label("Install status").classes("text-lg font-black mod-title-small")
                        state_label = ui.label("").classes(
                            "text-xs font-bold uppercase tracking-wide mod-subtitle"
                        )
                    with ui.row().classes("w-full items-center justify-between gap-3 flex-wrap"):
                        summary_label = ui.label("").classes("font-semibold")
                        progress_label = ui.label("").classes("mod-subtitle text-sm")
                    detail_label = ui.label("").classes("mod-subtitle text-sm break-words")
                    app_link = ui.link("Open app", "#").classes("text-sm font-semibold")
                    log_textarea = ui.textarea(value="").props(
                        "readonly filled square dense hide-bottom-space rows=8"
                    ).classes("w-full font-mono text-xs mod-config-input")
            status_card.set_visibility(False)
            progress_label.set_visibility(False)
            detail_label.set_visibility(False)
            app_link.set_visibility(False)
            log_textarea.set_visibility(False)
            return _AppInstallerStatusControls(
                card=status_card,
                state_label=state_label,
                summary_label=summary_label,
                progress_label=progress_label,
                detail_label=detail_label,
                app_link=app_link,
                log_textarea=log_textarea,
            )

        def update_status_view() -> None:
            controls = status_controls
            if controls is None:
                raise RuntimeError("App installer status controls are not ready.")
            status = state.status
            if status is None:
                controls.card.set_visibility(False)
                return
            controls.card.set_visibility(True)
            controls.state_label.set_text(status.state.value.replace("_", " ").title())
            controls.summary_label.set_text(status.summary)

            progress_percent = status.progress_percent
            controls.progress_label.set_visibility(progress_percent is not None)
            if progress_percent is not None:
                controls.progress_label.set_text(f"{progress_percent:.0f}%")

            detail = state.status_error or status.detail or ""
            controls.detail_label.set_visibility(bool(detail))
            controls.detail_label.set_text(detail)

            app_name = status.app_name
            controls.app_link.set_visibility(app_name is not None)
            if app_name is not None:
                app_path = self.node_app_path(status.node, app_name)
                if controls.app_path != app_path:
                    controls.app_link.props["href"] = app_path
                    controls.app_path = app_path

            log_text = "\n".join(status.log_lines[-12:])
            controls.log_textarea.set_visibility(bool(log_text))
            if controls.log_textarea.value != log_text:
                controls.log_textarea.set_value(log_text)

        async def refresh_page_lock() -> bool:
            lease = self._app_installer_page_lock.current()
            if lease is None or lease.job_id is None:
                return False
            node = nodes_by_name.get(lease.node_name.casefold())
            if node is None:
                return False
            try:
                status = await self._app_install_status(node=node, job_id=lease.job_id, user=user)
            except Exception as xcp:
                if lease.owner_token == page_token:
                    state.status_error = str(xcp) or type(xcp).__name__
                    update_status_view()
                return False
            if lease.owner_token == page_token:
                state.job_id = status.job_id
                state.status = status
                state.status_error = None
                update_status_view()
            if status.running:
                return False
            return self._app_installer_page_lock.release_completed_job(job_id=lease.job_id)

        async def poll_status() -> None:
            if state.status_polling:
                return
            state.status_polling = True
            try:
                lock_released = await refresh_page_lock()
            finally:
                state.status_polling = False
            if lock_released:
                render_wizard.refresh()

        def change_node(event: ValueChangeEventArguments[str | None]) -> None:
            if reject_when_wizard_locked():
                return
            requested_node_name = event.value
            if requested_node_name is None:
                ui.notify("Choose an available node.", type="warning")
                return
            if requested_node_name.casefold() not in nodes_by_name:
                ui.notify("Choose an available node.", type="warning")
                return
            if requested_node_name.casefold() == state.node_name.casefold():
                return
            state.node_name = nodes_by_name[requested_node_name.casefold()].node_name
            state.clear_job()
            state.catalog = None
            state.catalog_error = None
            state.apply_recipe(None)
            render_wizard.refresh()
            update_status_view()

        def change_recipe(event: ValueChangeEventArguments[str | None]) -> None:
            if reject_when_wizard_locked():
                return
            recipe = _recipe_from_catalog(catalog=state.catalog, scope=event.value)
            if recipe is None:
                ui.notify("Choose an available app.", type="warning")
                return
            state.apply_recipe(recipe)
            render_wizard.refresh()

        async def continue_to_app_step() -> None:
            if reject_when_wizard_locked():
                return
            state.step = _AppInstallerWizardStep.APP
            state.catalog = None
            state.catalog_error = None
            state.apply_recipe(None)
            render_wizard.refresh()
            await load_catalog()
            render_wizard.refresh()

        def continue_to_details_step() -> None:
            if reject_when_wizard_locked():
                return
            if _recipe_from_catalog(catalog=state.catalog, scope=state.recipe_scope) is None:
                ui.notify("Choose an app.", type="warning")
                return
            state.step = _AppInstallerWizardStep.DETAILS
            render_wizard.refresh()

        def back_to_node_step() -> None:
            if reject_when_wizard_locked():
                return
            state.step = _AppInstallerWizardStep.NODE
            render_wizard.refresh()

        def back_to_app_step() -> None:
            if reject_when_wizard_locked():
                return
            state.step = _AppInstallerWizardStep.APP
            render_wizard.refresh()

        def set_text(attribute: _AppInstallerTextField, event: ValueChangeEventArguments[str | None]) -> None:
            if wizard_is_locked():
                return
            setattr(state, attribute, "" if event.value is None else event.value)

        def set_input_value(key: AppInstallInput, event: ValueChangeEventArguments[str | None]) -> None:
            if wizard_is_locked():
                return
            state.inputs[key] = "" if event.value is None else event.value

        async def start_install() -> None:
            if await refresh_page_lock():
                render_wizard.refresh()
            if reject_when_wizard_locked():
                return
            recipe = _recipe_from_catalog(catalog=state.catalog, scope=state.recipe_scope)
            if recipe is None:
                ui.notify("Choose an app first.", type="warning")
                return
            try:
                request = NodeAppInstallRequest(
                    scope=recipe.scope,
                    instance_key=state.instance_key,
                    friendly_name=state.friendly_name,
                    subfolder=state.subfolder,
                    port=_optional_port(state.port_text),
                    steam_branch_id=state.steam_branch_id,
                    inputs=dict(state.inputs),
                )
            except Exception as xcp:
                ui.notify(f"Could not start install: {xcp}", type="negative", multi_line=True)
                return

            if not self._app_installer_page_lock.acquire(owner_token=page_token, node_name=state.node_name):
                ui.notify("Another dashboard session is already starting or running an install.", type="warning")
                render_wizard.refresh()
                return
            state.install_starting = True
            render_wizard.refresh()
            try:
                status = await self._start_app_install(node=selected_node(), request=request, user=user)
            except asyncio.CancelledError:
                self._app_installer_page_lock.release(owner_token=page_token)
                raise
            except Exception as xcp:
                self._app_installer_page_lock.release(owner_token=page_token)
                ui.notify(f"Could not start install: {xcp}", type="negative", multi_line=True)
            else:
                self._app_installer_page_lock.record_job(
                    owner_token=page_token,
                    node_name=status.node,
                    job_id=status.job_id,
                )
                state.job_id = status.job_id
                state.status = status
                state.status_error = None
                update_status_view()
                ui.notify("Install started.", type="positive")
            finally:
                state.install_starting = False
                render_wizard.refresh()

        node_options = {
            node.node_name: node.label if node.label.casefold() == node.node_name.casefold() else f"{node.label} · {node.node_name}"
            for node in nodes
        }

        @ui.refreshable
        def render_wizard() -> None:
            with ui.card().classes("mod-card w-full"):
                with ui.column().classes("w-full gap-4"):
                    if page_lock_is_held_by_other():
                        ui.label(
                            "Another dashboard session is starting or running an install. "
                            "This wizard will unlock when it completes."
                        ).classes("mod-subtitle text-sm")
                    ui.label(f"Step {state.step.number} of 3 · {state.step.label}").classes(
                        "text-sm font-black mod-title-small"
                    )
                    if state.step is _AppInstallerWizardStep.NODE:
                        node_select = ui.select(node_options, value=state.node_name, label="Node").props(
                            _INSTALL_SELECT_PROPS
                        ).classes("w-full md:max-w-md mod-app-details-field")
                        node_select.on_value_change(change_node)
                        next_button = ui.button("Next", icon="arrow_forward", on_click=continue_to_app_step).classes(
                            "mod-list-button self-start"
                        )
                        disable_when_wizard_locked(node_select, next_button)
                        return

                    if state.step is _AppInstallerWizardStep.APP:
                        can_continue = False
                        if state.catalog_error is not None:
                            ui.label("App choices could not be loaded.").classes("font-semibold")
                            ui.label(state.catalog_error).classes("mod-subtitle text-sm break-words")
                        else:
                            catalog = state.catalog
                            if catalog is None:
                                ui.label("Loading app choices.").classes("mod-subtitle")
                            elif not catalog.recipes:
                                ui.label("This node has no installable apps.").classes("mod-subtitle")
                            else:
                                recipe = _recipe_from_catalog(catalog=catalog, scope=state.recipe_scope)
                                if recipe is None:
                                    ui.label("Choose an app.").classes("mod-subtitle")
                                else:
                                    recipe_select = ui.select(
                                        {available.scope: available.label for available in catalog.recipes},
                                        value=recipe.scope,
                                        label="App",
                                    ).props(_INSTALL_SELECT_PROPS).classes(
                                        "w-full md:max-w-md mod-app-details-field"
                                    )
                                    recipe_select.on_value_change(change_recipe)
                                    disable_when_wizard_locked(recipe_select)
                                    can_continue = True
                        with ui.row().classes("items-center gap-2 flex-wrap"):
                            back_button = ui.button("Back", icon="arrow_back", on_click=back_to_node_step).classes(
                                "mod-list-button secondary"
                            )
                            disable_when_wizard_locked(back_button)
                            if can_continue:
                                next_button = ui.button(
                                    "Next",
                                    icon="arrow_forward",
                                    on_click=continue_to_details_step,
                                ).classes("mod-list-button")
                                disable_when_wizard_locked(next_button)
                        return

                    catalog = state.catalog
                    recipe = _recipe_from_catalog(catalog=catalog, scope=state.recipe_scope)
                    if recipe is None:
                        ui.label("App choice is no longer available.").classes("mod-subtitle")
                        back_button = ui.button("Back", icon="arrow_back", on_click=back_to_app_step).classes(
                            "mod-list-button secondary self-start"
                        )
                        disable_when_wizard_locked(back_button)
                        return

                    ui.label(f"{selected_node().label} · {recipe.label}").classes("mod-subtitle text-sm")
                    with ui.element("div").classes("w-full grid grid-cols-1 md:grid-cols-2 gap-3"):
                        instance_key_input = ui.input("Instance ID", value=state.instance_key).props(
                            _INSTALL_FIELD_PROPS
                        ).classes("w-full mod-app-details-field")
                        instance_key_input.on_value_change(lambda event: set_text("instance_key", event))
                        friendly_name_input = ui.input("Name", value=state.friendly_name).props(
                            _INSTALL_FIELD_PROPS
                        ).classes("w-full mod-app-details-field")
                        friendly_name_input.on_value_change(lambda event: set_text("friendly_name", event))
                        subfolder_input = ui.input("Install folder", value=state.subfolder).props(
                            _INSTALL_FIELD_PROPS
                        ).classes("w-full mod-app-details-field")
                        subfolder_input.on_value_change(lambda event: set_text("subfolder", event))
                        port_input = ui.input("Port", value=state.port_text).props(_INSTALL_FIELD_PROPS).classes(
                            "w-full mod-app-details-field"
                        )
                        port_input.on_value_change(lambda event: set_text("port_text", event))
                        branch_select = ui.select(
                            {branch.branch_id: branch.label for branch in recipe.branches},
                            value=state.steam_branch_id,
                            label="Version",
                        ).props(_INSTALL_SELECT_PROPS).classes("w-full mod-app-details-field")
                        branch_select.on_value_change(lambda event: set_text("steam_branch_id", event))
                        detail_controls: list[Input | Select] = [
                            instance_key_input,
                            friendly_name_input,
                            subfolder_input,
                            port_input,
                            branch_select,
                        ]
                        for install_input in recipe.fields:
                            input_key = AppInstallInput(install_input.key)
                            with ui.column().classes("w-full gap-1"):
                                input_control = ui.input(
                                    install_input.label,
                                    value=state.inputs.get(input_key, ""),
                                ).props(_INSTALL_FIELD_PROPS).classes("w-full mod-app-details-field")
                                if install_input.kind is NodeAppInstallInputKind.PASSWORD:
                                    input_control.props("type=password")
                                input_control.on_value_change(partial(set_input_value, input_key))
                                detail_controls.append(input_control)
                                if install_input.help_text is not None:
                                    ui.label(install_input.help_text).classes("mod-subtitle text-xs")
                    disable_when_wizard_locked(*detail_controls)
                    with ui.row().classes("items-center gap-2 flex-wrap"):
                        back_button = ui.button("Back", icon="arrow_back", on_click=back_to_app_step).classes(
                            "mod-list-button secondary"
                        )
                        install_button = ui.button("Install", icon="download", on_click=start_install).classes(
                            "mod-list-button"
                        )
                        disable_when_wizard_locked(back_button, install_button)

        render_wizard()
        status_controls = create_status_controls()
        update_status_view()
        refresh_timer: Timer = ui.timer(
            _INSTALL_STATUS_REFRESH_SECONDS,
            lambda: asyncio.create_task(poll_status()),
        )
        self._register_timer_cleanup(ui=ui, timer=refresh_timer)

    async def _app_install_catalog(
        self,
        *,
        node: ModWebNodeLink,
        user: ModWebUser,
    ) -> NodeAppInstallCatalog:
        if node.is_current:
            return self._node_api.build_app_install_catalog()
        payload = await self._remote_json_async(
            node=node,
            app_name=None,
            path="/app-installer",
            scopes=(NodeApiScope.APP_MANAGE,),
            user=user,
        )
        return NodeAppInstallCatalog.from_mapping(payload)

    async def _start_app_install(
        self,
        *,
        node: ModWebNodeLink,
        request: NodeAppInstallRequest,
        user: ModWebUser,
    ) -> NodeAppInstallStatus:
        if node.is_current:
            return await self._node_api.start_app_install(request=request, actor_user_id=user.discord_id)
        payload = await self._remote_json_async(
            node=node,
            app_name=None,
            path="/app-installer/jobs",
            scopes=(NodeApiScope.APP_MANAGE,),
            user=user,
            method="POST",
            json_payload=cast(dict[str, object], request.model_dump(mode="json")),
        )
        return NodeAppInstallStatus.from_mapping(payload)

    async def _app_install_status(
        self,
        *,
        node: ModWebNodeLink,
        job_id: str,
        user: ModWebUser,
    ) -> NodeAppInstallStatus:
        if node.is_current:
            return self._node_api.app_install_status(job_id=job_id)
        payload = await self._remote_json_async(
            node=node,
            app_name=None,
            path=f"/app-installer/jobs/{job_id}",
            scopes=(NodeApiScope.APP_MANAGE,),
            user=user,
        )
        return NodeAppInstallStatus.from_mapping(payload)


__all__: tuple[str, ...] = ("ModWebAppInstallerMixin",)

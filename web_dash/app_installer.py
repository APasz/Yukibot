"""Sudo-only, recipe-driven app installation page."""

from __future__ import annotations

import enum
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal, cast
from urllib.parse import quote
from uuid import uuid4

from nicegui.elements.card import Card

from _manager import AppInstallInput
from node_api.app_installer import (
    NodeAppInstallCatalog,
    NodeAppInstallInputKind,
    NodeAppInstallPreflight,
    NodeAppInstallRecipe,
    NodeAppInstallRequest,
    NodeAppInstallState,
    NodeAppInstallStatus,
)
from node_api.operation_service import NodeOperationView
from node_api.operations import NodeOperationKind
from node_auth import NodeApiScope

from .nicegui_protocols import ModWebNotificationType, ModWebUi
from .operations_ui import operation_detail_path
from .portal_app_installer import (
    PORTAL_APP_INSTALL_CONFLICT_MESSAGE,
    PortalAppInstallConflictError,
    PortalAppInstallCoordinator,
    PortalAppInstallOperation,
)
from .runtime_imports import Button, Card, Input, Label, ModWebUser, Select, Textarea, Timer, asyncio
from .service_base import ModWebServiceSupport
from .types import ModWebNodeLink
from .ui_helpers import ModWebUiHelpersMixin

if TYPE_CHECKING:
    from nicegui.client import Client
    from nicegui.elements.link import Link
    from nicegui.events import ValueChangeEventArguments

_INSTALL_STATUS_REFRESH_SECONDS = 1.0
_INSTALL_FIELD_PROPS = "filled square dense clearable hide-bottom-space color=accent"
_INSTALL_SELECT_PROPS = "filled square dense hide-bottom-space color=accent options-dark popup-content-class=mod-setting-menu"
_INSTALL_PREFLIGHT_SECRET_PLACEHOLDER = "preflight-secret"
_AppInstallerTextField = Literal["instance_key", "friendly_name", "subfolder", "port_text"]


def _notify_in_page_client(
    *,
    ui: ModWebUi,
    client: Client,
    message: str,
    tone: ModWebNotificationType,
    multi_line: bool = False,
) -> None:
    """Emit a notification outside a refreshable event handler's deleted slot."""
    with client:
        ui.notify(message, type=tone, multi_line=multi_line)


def _redact_install_error_detail(
    error: Exception,
    *,
    inputs: Mapping[AppInstallInput, str],
) -> str:
    """Remove submitted secret recipe values from a locally displayed error."""

    detail = str(error)
    secret_values: set[str] = set()
    for input_key, value in inputs.items():
        if input_key.is_secret:
            secret_values.update((value, value.strip()))
    secret_values.discard("")
    for value in sorted(secret_values, key=len, reverse=True):
        detail = detail.replace(value, "[REDACTED]")
    return detail


def _create_selectable_install_card(
    *,
    ui: ModWebUi,
    is_selected: bool,
    on_activate: Callable[[object | None], object],
) -> Card:
    card_classes = "mod-card mod-card-link w-full gap-2 cursor-pointer"
    if is_selected:
        card_classes += " border border-accent"
    card = ui.card().classes(card_classes).props(f"aria-pressed={'true' if is_selected else 'false'}")
    if is_selected:
        card.style("border-color: var(--mod-accent) !important")
    ModWebUiHelpersMixin._make_activatable(
        target=card,
        role="button",
        on_activate=on_activate,
    )
    return card


class _AppInstallerWizardStep(enum.StrEnum):
    NODE = "node"
    APP = "app"
    RELEASE = "release"
    REQUIREMENTS = "requirements"
    SERVER = "server"
    REVIEW = "review"

    @property
    def label(self) -> str:
        if self is _AppInstallerWizardStep.NODE:
            return "Node"
        if self is _AppInstallerWizardStep.APP:
            return "Game"
        if self is _AppInstallerWizardStep.RELEASE:
            return "Release"
        if self is _AppInstallerWizardStep.REQUIREMENTS:
            return "Requirements"
        if self is _AppInstallerWizardStep.SERVER:
            return "Server"
        if self is _AppInstallerWizardStep.REVIEW:
            return "Review"
        raise ValueError(f"Unknown app installer step: {self}")


@dataclass(slots=True)
class _AppInstallerPageState:
    node_name: str | None = None
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
    release_loading: bool = False
    release_error: str | None = None
    preflight_checking: bool = False
    preflight_message: str | None = None
    show_advanced_settings: bool = False
    instance_identity_customized: bool = False
    job_id: str | None = None
    status: NodeAppInstallStatus | None = None
    status_error: str | None = None
    status_polling: bool = False
    install_starting: bool = False
    install_cancelling: bool = False

    def apply_catalog(self, catalog: NodeAppInstallCatalog) -> None:
        self.catalog = catalog
        self.catalog_error = None
        selected_recipe = _recipe_from_catalog(catalog=catalog, scope=self.recipe_scope)
        self.apply_recipe(selected_recipe)

    def apply_recipe(self, recipe: NodeAppInstallRecipe | None) -> None:
        self.recipe_scope = None if recipe is None else recipe.scope
        self.release_loading = False
        self.release_error = None
        self.preflight_checking = False
        self.preflight_message = None
        self.show_advanced_settings = False
        self.instance_identity_customized = False
        if recipe is None:
            self.instance_key = ""
            self.friendly_name = ""
            self.subfolder = ""
            self.port_text = ""
            self.steam_branch_id = ""
            self.inputs.clear()
            return
        self.friendly_name = f"{recipe.label} Server"
        self.instance_key = recipe.default_instance_key
        self.subfolder = recipe.default_subfolder
        self.port_text = "" if recipe.default_port is None else str(recipe.default_port)
        self.steam_branch_id = recipe.default_branch_id
        self.inputs = {AppInstallInput(install_input.key): "" for install_input in recipe.fields}

    def apply_release_recipe(self, recipe: NodeAppInstallRecipe) -> None:
        """Replace the selected recipe with its freshly discovered releases."""

        catalog = self.catalog
        if catalog is None or self.recipe_scope is None:
            raise RuntimeError("A release recipe was loaded without an app selection.")
        if recipe.scope.casefold() != self.recipe_scope.casefold():
            raise ValueError("The loaded release options belong to a different app.")
        self.catalog = replace(
            catalog,
            recipes=tuple(
                recipe if current.scope.casefold() == recipe.scope.casefold() else current
                for current in catalog.recipes
            ),
        )
        previous_inputs = self.inputs
        next_inputs: dict[AppInstallInput, str] = {}
        for install_field in recipe.fields:
            input_key = AppInstallInput(install_field.key)
            next_inputs[input_key] = previous_inputs.get(input_key, "")
        self.inputs = next_inputs
        self.release_loading = False
        self.release_error = None
        if self.steam_branch_id.casefold() not in {branch.branch_id.casefold() for branch in recipe.branches}:
            self.steam_branch_id = recipe.default_branch_id
        if not self.instance_identity_customized:
            self.instance_key = recipe.default_instance_key
            self.subfolder = recipe.default_subfolder

    def clear_submitted_secrets(self) -> None:
        for input_key in self.inputs:
            if input_key.is_secret:
                self.inputs[input_key] = ""

    def clear_job(self) -> None:
        self.job_id = None
        self.status = None
        self.status_error = None
        self.status_polling = False
        self.install_starting = False
        self.install_cancelling = False


@dataclass(slots=True)
class _AppInstallerStatusControls:
    card: Card
    state_label: Label
    summary_label: Label
    progress_label: Label
    detail_label: Label
    app_link: Link
    log_textarea: Textarea
    cancel_button: Button
    new_install_button: Button
    app_path: str = "#"


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


def _preflight_install_request(request: NodeAppInstallRequest) -> NodeAppInstallRequest:
    """Replace write-only values before a non-mutating remote preflight check."""

    return request.model_copy(
        update={
            "inputs": {
                input_key: (_INSTALL_PREFLIGHT_SECRET_PLACEHOLDER if input_key.is_secret else value)
                for input_key, value in request.inputs.items()
            }
        }
    )


def _validate_preflight_result(
    *,
    result: NodeAppInstallPreflight,
    node_name: str,
    scope: str,
) -> None:
    """Ensure a remote preflight response still belongs to the selected target."""

    if result.node.casefold() != node_name.casefold():
        raise ValueError("The selected node returned a preflight result for a different node.")
    if result.scope.casefold() != scope.casefold():
        raise ValueError("The selected node returned a preflight result for a different app.")


class ModWebAppInstallerMixin(ModWebServiceSupport):
    """Render and broker recipe-backed installs across dashboard nodes."""

    _portal_app_install_coordinator: PortalAppInstallCoordinator = cast(
        PortalAppInstallCoordinator,
        cast(object, None),
    )

    def _observe_portal_app_install_operation(
        self,
        operation: PortalAppInstallOperation,
    ) -> bool:
        """Provide a standalone no-op hook when this UI mixin is unit-tested alone."""

        del operation
        return False

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
                        ui.label("Choose a node, game, release, and server setup.").classes("mod-subtitle text-sm")

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
        state = _AppInstallerPageState()
        page_token = uuid4().hex
        status_controls: _AppInstallerStatusControls | None = None
        catalog_request_id = 0
        release_request_id = 0
        from nicegui.context import context as nicegui_context

        notification_client = nicegui_context.client
        page_closed = False
        observed_portal_install_lease = self._portal_app_install_coordinator.current()

        def mark_page_closed() -> None:
            nonlocal page_closed
            page_closed = True

        self._register_client_cleanup(ui=ui, cleanup=mark_page_closed)

        def notify(
            message: str,
            *,
            tone: ModWebNotificationType,
            multi_line: bool = False,
        ) -> None:
            if page_closed:
                return
            _notify_in_page_client(
                ui=ui,
                client=notification_client,
                message=message,
                tone=tone,
                multi_line=multi_line,
            )

        def selected_node() -> ModWebNodeLink:
            node_name = state.node_name
            if node_name is None:
                raise RuntimeError("Choose an available node.")
            node = nodes_by_name.get(node_name.casefold())
            if node is None:
                raise RuntimeError("Selected node is no longer available.")
            return node

        def selected_recipe() -> NodeAppInstallRecipe | None:
            return _recipe_from_catalog(catalog=state.catalog, scope=state.recipe_scope)

        def invalidate_catalog_request() -> None:
            nonlocal catalog_request_id
            catalog_request_id += 1

        def invalidate_release_request() -> None:
            nonlocal release_request_id
            release_request_id += 1
            state.release_loading = False

        def catalog_request_is_current(*, request_id: int, node_name: str) -> bool:
            return (
                not page_closed
                and catalog_request_id == request_id
                and state.step is _AppInstallerWizardStep.APP
                and state.node_name is not None
                and state.node_name.casefold() == node_name.casefold()
            )

        def release_request_is_current(
            *,
            request_id: int,
            node_name: str,
            scope: str,
        ) -> bool:
            recipe = selected_recipe()
            return (
                not page_closed
                and release_request_id == request_id
                and state.step is _AppInstallerWizardStep.RELEASE
                and state.node_name is not None
                and state.node_name.casefold() == node_name.casefold()
                and recipe is not None
                and recipe.scope.casefold() == scope.casefold()
            )

        def wizard_steps() -> tuple[_AppInstallerWizardStep, ...]:
            recipe = selected_recipe()
            if recipe is None:
                return (_AppInstallerWizardStep.NODE, _AppInstallerWizardStep.APP)
            requirement_step = (_AppInstallerWizardStep.REQUIREMENTS,) if recipe.fields else ()
            return (
                _AppInstallerWizardStep.NODE,
                _AppInstallerWizardStep.APP,
                _AppInstallerWizardStep.RELEASE,
                *requirement_step,
                _AppInstallerWizardStep.SERVER,
                _AppInstallerWizardStep.REVIEW,
            )

        def selected_branch_label(recipe: NodeAppInstallRecipe) -> str:
            for branch in recipe.branches:
                if branch.branch_id.casefold() == state.steam_branch_id.casefold():
                    return branch.label
            return state.steam_branch_id

        def selected_branch_is_experimental(recipe: NodeAppInstallRecipe) -> bool:
            branch_text = f"{state.steam_branch_id} {selected_branch_label(recipe)}".casefold()
            return "experimental" in branch_text or "beta" in branch_text

        def install_is_active() -> bool:
            return state.install_starting or (state.status is not None and state.status.running)

        def portal_install_is_held_by_other() -> bool:
            return self._portal_app_install_lease_held_by_other(owner_token=page_token)

        def wizard_is_locked() -> bool:
            return state.preflight_checking or install_is_active()

        def install_start_is_locked() -> bool:
            return wizard_is_locked() or portal_install_is_held_by_other()

        def reject_when_wizard_locked() -> bool:
            if state.preflight_checking:
                notify("Checking the server setup.", tone="info")
                return True
            if install_is_active():
                notify("Wait for the install to finish.", tone="warning")
                return True
            return False

        def reject_when_install_start_locked() -> bool:
            if reject_when_wizard_locked():
                return True
            if portal_install_is_held_by_other():
                notify(PORTAL_APP_INSTALL_CONFLICT_MESSAGE, tone="warning")
                return True
            return False

        def disable_when_wizard_locked(*controls: Button | Input | Select) -> None:
            if wizard_is_locked():
                for control in controls:
                    control.disable()

        def disable_when_install_start_locked(*controls: Button) -> None:
            if install_start_is_locked():
                for control in controls:
                    control.disable()

        async def load_catalog() -> None:
            nonlocal catalog_request_id
            catalog_request_id += 1
            request_id = catalog_request_id
            requested_node = selected_node()
            try:
                catalog = await self._app_install_catalog(node=requested_node, user=user)
            except Exception as xcp:
                if not catalog_request_is_current(
                    request_id=request_id,
                    node_name=requested_node.node_name,
                ):
                    return
                state.catalog = None
                state.catalog_error = str(xcp) or type(xcp).__name__
                state.apply_recipe(None)
                return
            if not catalog_request_is_current(
                request_id=request_id,
                node_name=requested_node.node_name,
            ):
                return
            if catalog.node.casefold() != requested_node.node_name.casefold():
                state.catalog = None
                state.catalog_error = "The selected node returned app choices for a different node."
                state.apply_recipe(None)
                return
            state.apply_catalog(catalog)

        async def load_release_options() -> None:
            nonlocal release_request_id
            recipe = selected_recipe()
            if recipe is None:
                return
            release_request_id += 1
            request_id = release_request_id
            requested_node = selected_node()
            requested_scope = recipe.scope
            state.release_loading = True
            state.release_error = None
            render_wizard.refresh()
            try:
                release_recipe = await self._app_install_recipe(
                    node=requested_node,
                    scope=requested_scope,
                    user=user,
                )
                if not release_request_is_current(
                    request_id=request_id,
                    node_name=requested_node.node_name,
                    scope=requested_scope,
                ):
                    return
                state.apply_release_recipe(release_recipe)
            except asyncio.CancelledError:
                raise
            except Exception as xcp:
                if release_request_is_current(
                    request_id=request_id,
                    node_name=requested_node.node_name,
                    scope=requested_scope,
                ):
                    state.release_error = str(xcp) or type(xcp).__name__
            finally:
                if release_request_is_current(
                    request_id=request_id,
                    node_name=requested_node.node_name,
                    scope=requested_scope,
                ):
                    state.release_loading = False
                    render_wizard.refresh()

        async def cancel_install() -> None:
            status = state.status
            if (
                status is None
                or not status.running
                or status.state is NodeAppInstallState.CANCEL_REQUESTED
                or state.install_cancelling
            ):
                return
            state.install_cancelling = True
            update_status_view()
            try:
                cancelled_status = await self._cancel_app_install(
                    node=selected_node(),
                    job_id=status.job_id,
                    user=user,
                )
            except asyncio.CancelledError:
                raise
            except Exception as xcp:
                notify(f"Could not cancel install: {xcp}", tone="negative", multi_line=True)
            else:
                state.status = cancelled_status
                state.status_error = None
                if cancelled_status.state is NodeAppInstallState.CANCEL_REQUESTED:
                    notify("Install cancellation requested.", tone="warning")
                elif cancelled_status.state is NodeAppInstallState.CANCELLED:
                    notify("Install cancelled.", tone="warning")
                else:
                    notify("Install had already finished.", tone="info")
            finally:
                state.install_cancelling = False
                update_status_view()

        def start_new_install() -> None:
            status = state.status
            if status is None or status.running:
                return
            invalidate_catalog_request()
            invalidate_release_request()
            state.clear_job()
            state.catalog = None
            state.catalog_error = None
            state.apply_recipe(None)
            state.step = _AppInstallerWizardStep.NODE
            update_status_view()
            render_wizard.refresh()

        def create_status_controls() -> _AppInstallerStatusControls:
            with ui.card().classes("mod-card w-full") as status_card:
                with ui.column().classes("w-full gap-3"):
                    with ui.row().classes("w-full items-center justify-between gap-3 flex-wrap"):
                        ui.label("Install status").classes("text-lg font-black mod-title-small")
                        state_label = ui.label("").classes("text-xs font-bold uppercase tracking-wide mod-subtitle")
                    with ui.row().classes("w-full items-center justify-between gap-3 flex-wrap"):
                        summary_label = ui.label("").classes("font-semibold")
                        progress_label = ui.label("").classes("mod-subtitle text-sm")
                    detail_label = ui.label("").classes("mod-subtitle text-sm break-words")
                    app_link = ui.link("Open app", "#").classes("text-sm font-semibold")
                    log_textarea = (
                        ui.textarea(value="")
                        .props("readonly filled square dense hide-bottom-space rows=8")
                        .classes("w-full font-mono text-xs mod-config-input")
                    )
                    cancel_button = ui.button("Cancel install", icon="cancel", on_click=cancel_install).classes(
                        "mod-list-button secondary self-start"
                    )
                    new_install_button = ui.button(
                        "Install another app",
                        icon="add",
                        on_click=start_new_install,
                    ).classes("mod-list-button self-start")
            status_card.set_visibility(False)
            progress_label.set_visibility(False)
            detail_label.set_visibility(False)
            app_link.set_visibility(False)
            log_textarea.set_visibility(False)
            cancel_button.set_visibility(False)
            new_install_button.set_visibility(False)
            return _AppInstallerStatusControls(
                card=status_card,
                state_label=state_label,
                summary_label=summary_label,
                progress_label=progress_label,
                detail_label=detail_label,
                app_link=app_link,
                log_textarea=log_textarea,
                cancel_button=cancel_button,
                new_install_button=new_install_button,
            )

        def update_status_view() -> None:
            if page_closed:
                return
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

            can_cancel = (
                status.running
                and status.state is not NodeAppInstallState.CANCEL_REQUESTED
                and not state.install_cancelling
            )
            controls.cancel_button.set_visibility(status.running)
            controls.cancel_button.set_enabled(can_cancel)
            controls.new_install_button.set_visibility(not status.running)

        async def refresh_install_status() -> bool:
            status = state.status
            if status is None or not status.running:
                return False
            node = nodes_by_name.get(status.node.casefold())
            if node is None:
                state.status_error = "The install node is no longer available."
                update_status_view()
                return False
            try:
                refreshed_status = await self._app_install_status(
                    node=node,
                    job_id=status.job_id,
                    user=user,
                )
            except Exception as xcp:
                state.status_error = str(xcp) or type(xcp).__name__
                update_status_view()
                return False
            changed = state.status != refreshed_status or state.status_error is not None
            state.job_id = refreshed_status.job_id
            state.status = refreshed_status
            state.status_error = None
            update_status_view()
            return changed

        async def poll_status() -> None:
            nonlocal observed_portal_install_lease
            if state.status_polling:
                return
            state.status_polling = True
            try:
                status_changed = await refresh_install_status()
            finally:
                state.status_polling = False
            current_lease = self._portal_app_install_coordinator.current()
            lease_changed = observed_portal_install_lease != current_lease
            observed_portal_install_lease = current_lease
            if (status_changed or lease_changed) and not page_closed:
                render_wizard.refresh()

        async def choose_node(requested_node_name: str) -> None:
            if reject_when_wizard_locked():
                return
            if requested_node_name.casefold() not in nodes_by_name:
                notify("Choose an available node.", tone="warning")
                return
            if state.node_name is None or requested_node_name.casefold() != state.node_name.casefold():
                invalidate_catalog_request()
                state.node_name = nodes_by_name[requested_node_name.casefold()].node_name
                state.clear_job()
                update_status_view()
            await continue_to_app_step()

        async def choose_recipe(scope: str) -> None:
            if reject_when_wizard_locked():
                return
            recipe = _recipe_from_catalog(catalog=state.catalog, scope=scope)
            if recipe is None:
                notify("Choose an available app.", tone="warning")
                return
            invalidate_release_request()
            state.apply_recipe(recipe)
            state.step = _AppInstallerWizardStep.RELEASE
            render_wizard.refresh()
            await load_release_options()

        async def continue_to_app_step() -> None:
            if reject_when_wizard_locked():
                return
            invalidate_release_request()
            state.step = _AppInstallerWizardStep.APP
            state.catalog = None
            state.catalog_error = None
            state.apply_recipe(None)
            render_wizard.refresh()
            await load_catalog()
            if not page_closed:
                render_wizard.refresh()

        def continue_from_release_step() -> None:
            if reject_when_wizard_locked():
                return
            recipe = selected_recipe()
            if recipe is None:
                notify("Choose an app.", tone="warning")
                return
            if state.steam_branch_id.casefold() not in {branch.branch_id.casefold() for branch in recipe.branches}:
                notify("Choose an available release.", tone="warning")
                return
            state.step = _AppInstallerWizardStep.REQUIREMENTS if recipe.fields else _AppInstallerWizardStep.SERVER
            render_wizard.refresh()

        def validate_requirements(recipe: NodeAppInstallRecipe) -> str | None:
            for install_field in recipe.fields:
                input_key = AppInstallInput(install_field.key)
                value = state.inputs.get(input_key, "")
                if install_field.required and not value.strip():
                    return f"{install_field.label} is required."
                if input_key is AppInstallInput.GAME_SERVER_LOGIN_TOKEN and any(
                    character.isspace() for character in value
                ):
                    return "Steam Game Server Login Token (GSLT) must not contain whitespace."
            return None

        def continue_from_requirements_step() -> None:
            if reject_when_wizard_locked():
                return
            recipe = selected_recipe()
            if recipe is None:
                notify("Choose an app.", tone="warning")
                return
            if requirement_error := validate_requirements(recipe):
                notify(requirement_error, tone="warning")
                return
            state.step = _AppInstallerWizardStep.SERVER
            render_wizard.refresh()

        def build_install_request(recipe: NodeAppInstallRecipe) -> NodeAppInstallRequest:
            return NodeAppInstallRequest(
                scope=recipe.scope,
                instance_key=state.instance_key,
                friendly_name=state.friendly_name,
                subfolder=state.subfolder,
                port=_optional_port(state.port_text),
                steam_branch_id=state.steam_branch_id,
                inputs=dict(state.inputs),
            )

        async def continue_to_review_step() -> None:
            if reject_when_wizard_locked():
                return
            recipe = selected_recipe()
            if recipe is None:
                notify("Choose an app.", tone="warning")
                return
            requested_node = selected_node()
            requested_scope = recipe.scope
            try:
                install_request = build_install_request(recipe)
            except Exception as xcp:
                detail = _redact_install_error_detail(xcp, inputs=state.inputs)
                notify(f"Check the server setup: {detail}", tone="warning", multi_line=True)
                return
            state.preflight_checking = True
            state.preflight_message = None
            render_wizard.refresh()
            try:
                result = await self._preflight_app_install(
                    node=requested_node,
                    request=_preflight_install_request(install_request),
                    user=user,
                )
                _validate_preflight_result(
                    result=result,
                    node_name=requested_node.node_name,
                    scope=requested_scope,
                )
            except asyncio.CancelledError:
                raise
            except Exception as xcp:
                detail = _redact_install_error_detail(xcp, inputs=state.inputs)
                notify(f"Check the server setup: {detail}", tone="warning", multi_line=True)
            else:
                state.preflight_message = result.message
                state.step = _AppInstallerWizardStep.REVIEW
            finally:
                state.preflight_checking = False
                if not page_closed:
                    render_wizard.refresh()

        def back_to_node_step() -> None:
            if reject_when_wizard_locked():
                return
            invalidate_catalog_request()
            state.step = _AppInstallerWizardStep.NODE
            render_wizard.refresh()

        def back_to_app_step() -> None:
            if reject_when_wizard_locked():
                return
            invalidate_release_request()
            state.step = _AppInstallerWizardStep.APP
            render_wizard.refresh()

        def back_to_release_step() -> None:
            if reject_when_wizard_locked():
                return
            state.step = _AppInstallerWizardStep.RELEASE
            render_wizard.refresh()

        def back_from_server_step() -> None:
            if reject_when_wizard_locked():
                return
            recipe = selected_recipe()
            state.step = (
                _AppInstallerWizardStep.REQUIREMENTS
                if recipe is not None and recipe.fields
                else _AppInstallerWizardStep.RELEASE
            )
            render_wizard.refresh()

        def back_to_server_step() -> None:
            if reject_when_wizard_locked():
                return
            state.step = _AppInstallerWizardStep.SERVER
            render_wizard.refresh()

        def set_text(attribute: _AppInstallerTextField, event: ValueChangeEventArguments[str | None]) -> None:
            if wizard_is_locked():
                return
            value = "" if event.value is None else event.value
            setattr(state, attribute, value)
            if attribute in {"instance_key", "subfolder"}:
                state.instance_identity_customized = True

        def set_input_value(key: AppInstallInput, event: ValueChangeEventArguments[str | None]) -> None:
            if wizard_is_locked():
                return
            state.inputs[key] = "" if event.value is None else event.value

        def change_release(event: ValueChangeEventArguments[str | None]) -> None:
            if wizard_is_locked():
                return
            recipe = selected_recipe()
            if recipe is None or event.value is None:
                notify("Choose an available release.", tone="warning")
                return
            if event.value.casefold() not in {branch.branch_id.casefold() for branch in recipe.branches}:
                notify("Choose an available release.", tone="warning")
                return
            state.steam_branch_id = event.value
            render_wizard.refresh()

        def toggle_advanced_settings() -> None:
            if reject_when_wizard_locked():
                return
            state.show_advanced_settings = not state.show_advanced_settings
            render_wizard.refresh()

        def reset_automatic_identity() -> None:
            if reject_when_wizard_locked():
                return
            recipe = selected_recipe()
            if recipe is None:
                return
            state.instance_key = recipe.default_instance_key
            state.subfolder = recipe.default_subfolder
            state.instance_identity_customized = False
            render_wizard.refresh()

        async def start_install() -> None:
            if page_closed:
                return
            if reject_when_install_start_locked():
                return
            recipe = selected_recipe()
            if recipe is None:
                notify("Choose an app first.", tone="warning")
                return
            if requirement_error := validate_requirements(recipe):
                notify(requirement_error, tone="warning")
                return
            try:
                request = build_install_request(recipe)
            except Exception as xcp:
                detail = _redact_install_error_detail(xcp, inputs=state.inputs)
                notify(f"Could not start install: {detail}", tone="negative", multi_line=True)
                return

            state.install_starting = True
            render_wizard.refresh()
            try:
                status = await self._start_app_install(
                    node=selected_node(),
                    request=request,
                    user=user,
                    owner_token=page_token,
                )
            except asyncio.CancelledError:
                raise
            except PortalAppInstallConflictError:
                notify(PORTAL_APP_INSTALL_CONFLICT_MESSAGE, tone="warning")
            except Exception as xcp:
                detail = _redact_install_error_detail(xcp, inputs=state.inputs)
                notify(f"Could not start install: {detail}", tone="negative", multi_line=True)
            else:
                state.job_id = status.job_id
                state.status = status
                state.status_error = None
                state.clear_submitted_secrets()
                if not page_closed:
                    update_status_view()
                notify("Install started.", tone="positive")
            finally:
                state.install_starting = False
                if not page_closed:
                    render_wizard.refresh()

        status_controls = create_status_controls()

        @ui.refreshable
        def render_wizard() -> None:
            if state.status is not None:
                return
            with ui.card().classes("mod-card w-full"):
                with ui.column().classes("w-full gap-4"):
                    lease = self._portal_app_install_coordinator.current()
                    if portal_install_is_held_by_other() and lease is not None:
                        ui.label(PORTAL_APP_INSTALL_CONFLICT_MESSAGE).classes("mod-subtitle text-sm")
                        if lease.unresolved_recovery_conflict:
                            ui.label(
                                "Portal is reconciling install state and waiting for all nodes to be reachable."
                            ).classes("mod-subtitle text-sm")
                        ui.label(
                            "You can continue configuring this install, but it cannot start yet."
                        ).classes("mod-subtitle text-sm")
                        if lease.node_name is not None and lease.operation_id is not None:
                            ui.link(
                                "View active install",
                                operation_detail_path(
                                    node_name=lease.node_name,
                                    operation_id=lease.operation_id,
                                ),
                            ).classes("text-sm font-semibold")
                    if state.install_starting:
                        ui.label("Starting install…").classes("text-lg font-black mod-title-small")
                        ui.label("The installer is preparing its job.").classes("mod-subtitle text-sm")
                        return
                    steps = wizard_steps()
                    if state.step not in steps:
                        state.step = steps[-1]
                    step_number: int = steps.index(state.step) + 1
                    ui.label(f"Step {step_number} of {len(steps)} · {state.step.label}").classes(
                        "text-sm font-black mod-title-small"
                    )
                    if state.step is _AppInstallerWizardStep.NODE:
                        with ui.element("div").classes("w-full grid grid-cols-1 md:grid-cols-2 gap-3"):
                            for available_node in nodes:
                                is_selected: bool = (
                                    state.node_name is not None
                                    and state.node_name.casefold() == available_node.node_name.casefold()
                                )
                                node_card: Card = _create_selectable_install_card(
                                    ui=ui,
                                    is_selected=is_selected,
                                    on_activate=(
                                        lambda _event=None, node_name=available_node.node_name: choose_node(node_name)
                                    ),
                                )
                                with node_card:
                                    with ui.row().classes("items-center gap-3"):
                                        ui.html(
                                            self._node_bot_avatar_markup(
                                                node_name=available_node.node_name,
                                                display_name=available_node.label,
                                                extra_class="mod-app-installer-node-avatar",
                                            )
                                        ).classes("shrink-0")
                                        with ui.column().classes("gap-0 min-w-0"):
                                            node_label = ui.label(available_node.label).classes("font-bold")
                                            if node_text_style := self._node_text_style(
                                                node_name=available_node.node_name
                                            ):
                                                node_label.style(node_text_style)
                                            if available_node.label.casefold() != available_node.node_name.casefold():
                                                ui.label(available_node.node_name).classes("mod-subtitle text-sm")
                        return

                    if state.step is _AppInstallerWizardStep.APP:
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
                                recipe = selected_recipe()
                                if recipe is None:
                                    ui.label("Choose a game.").classes("mod-subtitle")
                                with ui.element("div").classes(
                                    "w-full grid grid-cols-1 md:grid-cols-2 gap-3"
                                ):
                                    for available in catalog.recipes:
                                        is_selected = (
                                            recipe is not None
                                            and recipe.scope.casefold() == available.scope.casefold()
                                        )
                                        app_card = _create_selectable_install_card(
                                            ui=ui,
                                            is_selected=is_selected,
                                            on_activate=(
                                                lambda _event=None, scope=available.scope: choose_recipe(scope)
                                            ),
                                        )
                                        with app_card:
                                            ui.label(available.label).classes("font-bold")
                        with ui.row().classes("items-center gap-2 flex-wrap"):
                            back_button = ui.button("Back", icon="arrow_back", on_click=back_to_node_step).classes(
                                "mod-list-button secondary"
                            )
                            disable_when_wizard_locked(back_button)
                        return

                    recipe = selected_recipe()
                    if recipe is None:
                        ui.label("App choice is no longer available.").classes("mod-subtitle")
                        back_button = ui.button("Back", icon="arrow_back", on_click=back_to_app_step).classes(
                            "mod-list-button secondary self-start"
                        )
                        disable_when_wizard_locked(back_button)
                        return

                    ui.label(f"{selected_node().label} · {recipe.label}").classes("mod-subtitle text-sm")

                    if state.step is _AppInstallerWizardStep.RELEASE:
                        ui.label("Choose the Steam release to install.").classes("font-semibold")
                        if state.release_loading:
                            ui.label("Loading current release options…").classes("mod-subtitle text-sm")
                        elif state.release_error is not None:
                            ui.label("Could not refresh release options; using cached choices.").classes(
                                "mod-subtitle text-sm"
                            )
                            ui.label(state.release_error).classes("mod-subtitle text-xs break-words")
                        branch_select = ui.select(
                            {branch.branch_id: branch.label for branch in recipe.branches},
                            value=state.steam_branch_id,
                            label="Release / Steam branch",
                        ).props(_INSTALL_SELECT_PROPS).classes("w-full md:max-w-md mod-app-details-field")
                        branch_select.on_value_change(change_release)
                        ui.label(
                            f"Node default: {next((branch.label for branch in recipe.branches if branch.branch_id.casefold() == recipe.default_branch_id.casefold()), recipe.default_branch_id)}"
                        ).classes("mod-subtitle text-xs")
                        if selected_branch_is_experimental(recipe):
                            ui.label(
                                "This selection appears to be an experimental or beta release."
                            ).classes("text-warning text-sm")
                        with ui.row().classes("items-center gap-2 flex-wrap"):
                            back_button = ui.button("Back", icon="arrow_back", on_click=back_to_app_step).classes(
                                "mod-list-button secondary"
                            )
                            next_button = ui.button(
                                "Next",
                                icon="arrow_forward",
                                on_click=continue_from_release_step,
                            ).classes("mod-list-button")
                            disable_when_wizard_locked(back_button, branch_select, next_button)
                            if state.release_loading:
                                next_button.disable()
                        return

                    if state.step is _AppInstallerWizardStep.REQUIREMENTS:
                        ui.label(f"{recipe.label} requirements").classes("font-semibold")
                        ui.label(
                            "These values are used only for this install. Secret values are never shown again."
                        ).classes("mod-subtitle text-sm")
                        requirement_controls: list[Input] = []
                        for install_field in recipe.fields:
                            input_key = AppInstallInput(install_field.key)
                            with ui.column().classes("w-full gap-1"):
                                input_control = ui.input(
                                    install_field.label,
                                    value=state.inputs.get(input_key, ""),
                                ).props(_INSTALL_FIELD_PROPS).classes("w-full md:max-w-xl mod-app-details-field")
                                if install_field.kind is NodeAppInstallInputKind.PASSWORD:
                                    input_control.props("type=password autocomplete=off")
                                input_control.on_value_change(
                                    lambda event, key=input_key: set_input_value(key, event)
                                )
                                requirement_controls.append(input_control)
                                if install_field.help_text is not None:
                                    ui.label(install_field.help_text).classes("mod-subtitle text-xs")
                                if install_field.game_server_login_token_app_id is not None:
                                    ui.label(
                                        f"Required Steam App ID: {install_field.game_server_login_token_app_id}"
                                    ).classes("mod-subtitle text-xs")
                                if install_field.action_label is not None and install_field.action_url is not None:
                                    ui.link(
                                        install_field.action_label,
                                        install_field.action_url,
                                        new_tab=True,
                                    ).classes("text-sm font-semibold")
                        with ui.row().classes("items-center gap-2 flex-wrap"):
                            back_button = ui.button(
                                "Back",
                                icon="arrow_back",
                                on_click=back_to_release_step,
                            ).classes("mod-list-button secondary")
                            next_button = ui.button(
                                "Next",
                                icon="arrow_forward",
                                on_click=continue_from_requirements_step,
                            ).classes("mod-list-button")
                            disable_when_wizard_locked(back_button, next_button, *requirement_controls)
                        return

                    if state.step is _AppInstallerWizardStep.SERVER:
                        ui.label("Set up the server").classes("font-semibold")
                        ui.label(
                            "The next instance ID is chosen from this app's existing instances. You can change it or the install folder."
                        ).classes("mod-subtitle text-sm")
                        with ui.element("div").classes("w-full grid grid-cols-1 md:grid-cols-2 gap-3"):
                            friendly_name_input = ui.input("Server name", value=state.friendly_name).props(
                                _INSTALL_FIELD_PROPS
                            ).classes("w-full mod-app-details-field")
                            friendly_name_input.on_value_change(
                                lambda event: set_text("friendly_name", event)
                            )
                            port_input = ui.input("Game port", value=state.port_text).props(
                                _INSTALL_FIELD_PROPS
                            ).classes("w-full mod-app-details-field")
                            port_input.on_value_change(lambda event: set_text("port_text", event))
                        ui.label(
                            "Some games reserve related network ports as part of their server setup."
                        ).classes("mod-subtitle text-xs")
                        if state.preflight_checking:
                            ui.label("Checking the server setup…").classes("mod-subtitle text-sm")
                        advanced_toggle = ui.button(
                            "Hide advanced settings" if state.show_advanced_settings else "Show advanced settings",
                            icon="expand_less" if state.show_advanced_settings else "expand_more",
                            on_click=toggle_advanced_settings,
                        ).classes("mod-list-button secondary self-start")
                        server_controls: list[Button | Input] = [
                            friendly_name_input,
                            port_input,
                            advanced_toggle,
                        ]
                        if state.show_advanced_settings:
                            with ui.element("div").classes("w-full grid grid-cols-1 md:grid-cols-2 gap-3"):
                                instance_key_input = ui.input("Instance ID", value=state.instance_key).props(
                                    _INSTALL_FIELD_PROPS
                                ).classes("w-full mod-app-details-field")
                                instance_key_input.on_value_change(
                                    lambda event: set_text("instance_key", event)
                                )
                                subfolder_input = ui.input("Install folder", value=state.subfolder).props(
                                    _INSTALL_FIELD_PROPS
                                ).classes("w-full mod-app-details-field")
                                subfolder_input.on_value_change(lambda event: set_text("subfolder", event))
                            server_controls.extend((instance_key_input, subfolder_input))
                            if state.instance_identity_customized:
                                reset_button = ui.button(
                                    "Reset suggested IDs",
                                    icon="restart_alt",
                                    on_click=reset_automatic_identity,
                                ).classes("mod-list-button secondary self-start")
                                server_controls.append(reset_button)
                        with ui.row().classes("items-center gap-2 flex-wrap"):
                            back_button = ui.button(
                                "Back",
                                icon="arrow_back",
                                on_click=back_from_server_step,
                            ).classes("mod-list-button secondary")
                            next_button = ui.button(
                                "Review install",
                                icon="arrow_forward",
                                on_click=continue_to_review_step,
                            ).classes("mod-list-button")
                            disable_when_wizard_locked(back_button, next_button, *server_controls)
                        return

                    if state.step is _AppInstallerWizardStep.REVIEW:
                        ui.label("Review install").classes("font-semibold")
                        if state.preflight_message is not None:
                            ui.label(state.preflight_message).classes("text-positive text-sm")
                        ui.label(
                            "Check these choices before the installer begins. The node rechecks folder and instance availability when it starts."
                        ).classes("mod-subtitle text-sm")
                        with ui.column().classes("w-full gap-2"):
                            for label, value in (
                                ("Node", selected_node().label),
                                ("Game", recipe.label),
                                ("Release", selected_branch_label(recipe)),
                                ("Server name", state.friendly_name),
                                ("Game port", state.port_text or "Use game default"),
                                ("Instance ID", state.instance_key),
                                ("Install folder", state.subfolder),
                            ):
                                with ui.row().classes("w-full items-baseline justify-between gap-4 flex-wrap"):
                                    ui.label(label).classes("mod-subtitle text-sm")
                                    ui.label(value).classes("font-semibold text-sm break-all")
                            for install_field in recipe.fields:
                                ui.label(f"{install_field.label}: ready").classes("mod-subtitle text-sm")
                        with ui.row().classes("items-center gap-2 flex-wrap"):
                            back_button = ui.button(
                                "Back",
                                icon="arrow_back",
                                on_click=back_to_server_step,
                            ).classes("mod-list-button secondary")
                            install_button = ui.button(
                                "Install",
                                icon="download",
                                on_click=start_install,
                            ).classes("mod-list-button")
                            disable_when_wizard_locked(back_button)
                            disable_when_install_start_locked(install_button)

        render_wizard()
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
            return await self._node_api.app_installer.build_catalog()
        payload = await self._remote_json_async(
            node=node,
            app_name=None,
            path="/app-installer",
            scopes=(NodeApiScope.APP_MANAGE,),
            user=user,
        )
        return NodeAppInstallCatalog.from_mapping(payload)

    async def _app_install_recipe(
        self,
        *,
        node: ModWebNodeLink,
        scope: str,
        user: ModWebUser,
    ) -> NodeAppInstallRecipe:
        """Load one app's current release options after it has been selected."""

        if node.is_current:
            return await self._node_api.app_installer.build_recipe(scope=scope)
        payload = await self._remote_json_async(
            node=node,
            app_name=None,
            path=f"/app-installer/apps/{quote(scope, safe='')}",
            scopes=(NodeApiScope.APP_MANAGE,),
            user=user,
        )
        return NodeAppInstallRecipe.from_mapping(payload)

    async def _preflight_app_install(
        self,
        *,
        node: ModWebNodeLink,
        request: NodeAppInstallRequest,
        user: ModWebUser,
    ) -> NodeAppInstallPreflight:
        """Validate the selected target before the user confirms installation."""

        if node.is_current:
            return await self._node_api.app_installer.preflight_install(
                request=request,
                actor_user_id=user.discord_id,
            )
        payload = await self._remote_json_async(
            node=node,
            app_name=None,
            path="/app-installer/preflight",
            scopes=(NodeApiScope.APP_MANAGE,),
            user=user,
            method="POST",
            json_payload=cast(dict[str, object], request.model_dump(mode="json")),
        )
        return NodeAppInstallPreflight.from_mapping(payload)

    async def _start_app_install(
        self,
        *,
        node: ModWebNodeLink,
        request: NodeAppInstallRequest,
        user: ModWebUser,
        owner_token: str | None = None,
    ) -> NodeAppInstallStatus:
        """Start through the Portal coordinator before contacting the selected node."""

        return await self._start_portal_app_install(
            node=node,
            owner_token=owner_token,
            start_install=lambda: self._start_node_app_install(
                node=node,
                request=request,
                user=user,
            ),
        )

    async def _start_node_app_install(
        self,
        *,
        node: ModWebNodeLink,
        request: NodeAppInstallRequest,
        user: ModWebUser,
    ) -> NodeAppInstallStatus:
        """Call the lower-level node-local installer endpoint after Portal approval."""

        if node.is_current:
            return await self._node_api.app_installer.start_install(
                request=request,
                actor_user_id=user.discord_id,
            )
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
            operation = self._node_api.operation_api.get_operation(
                operation_id=job_id,
                kind=NodeOperationKind.APP_INSTALL,
            )
        else:
            payload = await self._remote_json_async(
                node=node,
                app_name=None,
                path=f"/operations/{quote(job_id, safe='')}?kind={NodeOperationKind.APP_INSTALL.value}",
                scopes=(NodeApiScope.APP_MANAGE,),
                user=user,
            )
            operation = NodeOperationView.from_mapping(payload)
        record = operation.record
        if record.node_name.casefold() != node.node_name.casefold():
            raise ValueError("The selected node returned an install operation for a different node.")
        self._observe_portal_app_install_operation(
            PortalAppInstallOperation(
                node_name=record.node_name,
                operation_id=record.operation_id,
                state=record.state,
            )
        )
        return NodeAppInstallStatus.from_operation(record)

    async def _cancel_app_install(
        self,
        *,
        node: ModWebNodeLink,
        job_id: str,
        user: ModWebUser,
    ) -> NodeAppInstallStatus:
        if node.is_current:
            await self._node_api.operation_api.cancel_operation(
                operation_id=job_id,
                actor_user_id=user.discord_id,
                kind=NodeOperationKind.APP_INSTALL,
            )
        else:
            await self._remote_json_async(
                node=node,
                app_name=None,
                path=(
                    f"/operations/{quote(job_id, safe='')}/cancel?"
                    f"kind={NodeOperationKind.APP_INSTALL.value}"
                ),
                scopes=(NodeApiScope.APP_MANAGE,),
                user=user,
                method="POST",
                json_payload={},
            )
        return await self._app_install_status(node=node, job_id=job_id, user=user)


__all__: tuple[str, ...] = ("ModWebAppInstallerMixin",)

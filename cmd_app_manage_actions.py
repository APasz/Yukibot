from __future__ import annotations

from datetime import datetime
from typing import Protocol

import hikari
from hikari_ui import EditorFileUpload, EditorRequest

import _errors
from _discord import Distils, FileDeliveryMode
from _manager import App_Manager
from _mod_ops import (
    compress_mod_archive_entries,
    download_entries as build_mod_download_entries,
    install_attachments,
    require_app_stopped_for_mod_mutation,
)
from _security import Access_Control
from _sys import Stats_System
from _utils import Utilities
from apps._app import App
from apps._mod import Mod
from cmd_app_manage import (
    AppManageMode,
    AppManageState,
    AppManagementLock,
    EditorStatus,
    ModUploadRequestMeta,
    _coerce_status,
    _current_node_app_url,
    _error_status,
    _mod_upload_meta_from_mapping,
    _status_text,
    log,
)


class AppManageActionHost(Protocol):
    def manage_lock_reason(self, app: App, *, message_id: hikari.Snowflake | None = None) -> str | None: ...

    def _interaction_message_id(self, interaction: hikari.ComponentInteraction) -> hikari.Snowflake | None: ...

    def _interaction_guild_id(
        self,
        interaction: hikari.ComponentInteraction | hikari.ModalInteraction,
    ) -> hikari.Snowflake | None: ...

    async def _edit_editor_message(
        self,
        *,
        interaction: hikari.ComponentInteraction,
        actor_user_id: int,
        locale: hikari.Locale,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        status: EditorStatus | str,
    ) -> None: ...

    def _state_for_mod(
        self,
        *,
        app: App,
        mod_name: str,
        fallback: AppManageState,
    ) -> AppManageState: ...

    def _touch_app_lock(
        self,
        *,
        message_id: hikari.Snowflake,
        user_id: hikari.Snowflakeish,
        app_name: str,
        channel_id: hikari.Snowflakeish | None = None,
        guild_id: hikari.Snowflakeish | None = None,
        application_id: hikari.Snowflakeish | None = None,
        interaction_token: str | None = None,
        response_expires_at: object | None = None,
        now: object | None = None,
    ) -> None: ...

    def _extend_editor_session(self, message_id: hikari.Snowflake) -> None: ...

    def _render_editor(
        self,
        *,
        actor_user_id: int,
        locale: hikari.Locale,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        status: EditorStatus | str | None,
        current_guild_id: hikari.Snowflakeish | None = None,
    ) -> tuple[hikari.Embed | None, list[hikari.api.MessageActionRowBuilder]]: ...

    def _clear_pending_upload_request(
        self,
        *,
        channel_id: hikari.Snowflakeish | None,
        user_id: hikari.Snowflakeish,
    ) -> None: ...

    def _release_app_lock(self, *, message_id: hikari.Snowflakeish) -> None: ...

    def _now(self) -> datetime: ...


async def handle_app_download_action(
    host: AppManageActionHost,
    *,
    req: EditorRequest,
    acl: Access_Control,
    manager: App_Manager,
    state: AppManageState,
    app: App,
) -> None:
    await req.interaction.create_initial_response(hikari.ResponseType.DEFERRED_MESSAGE_UPDATE)
    try:
        if reason := host.manage_lock_reason(app, message_id=host._interaction_message_id(req.interaction)):
            raise RuntimeError(reason)
        if not app.directory.exists():
            raise FileNotFoundError(f"{app.directory} does not exist")

        size = Distils.file.pointer_size(app.directory)
        padded_size = round(size + (size / 100 * 10))
        stats = Stats_System()
        stats.update()
        disk = stats.disk_for_path(app.directory) or stats.primary_disk
        if disk is None:
            raise RuntimeError("No disk information is available for this app directory.")
        free_space = disk.usage.free
        if free_space < padded_size:
            raise _errors.NotEnoughDisk(
                f"{Utilities.humanise_bytes(free_space)} < {Utilities.humanise_bytes(padded_size)}"
            )

        download_message = await Distils.build_direct_file_message([app.directory], app.friendly)
        status = f"Prepared download for `{app.friendly}`.\n{download_message}"
    except Exception as xcp:
        status = _error_status(f"Error: download failed for `{app.friendly}`: {xcp}")

    await host._edit_editor_message(
        interaction=req.interaction,
        actor_user_id=int(req.user_id),
        locale=req.locale,
        acl=acl,
        manager=manager,
        state=state,
        status=status,
    )


async def handle_mod_download_action(
    host: AppManageActionHost,
    *,
    req: EditorRequest,
    acl: Access_Control,
    manager: App_Manager,
    state: AppManageState,
    app: App,
    selected_mod: Mod | None,
) -> None:
    await req.interaction.create_initial_response(hikari.ResponseType.DEFERRED_MESSAGE_UPDATE)
    try:
        if reason := host.manage_lock_reason(app, message_id=host._interaction_message_id(req.interaction)):
            raise RuntimeError(reason)
        if app.mods is None:
            raise _errors.UnsupportedModManager(app.friendly)
        channel_id = getattr(req.interaction, "channel_id", None)
        if channel_id is None:
            raise RuntimeError("Download delivery requires a channel.")

        mod_names = None if selected_mod is None else (selected_mod.name,)
        entries = build_mod_download_entries(
            app.has_mod_manager,
            mod_names,
            default_enabled_only=False,
        )
        if not entries:
            raise FileNotFoundError(f"No mods found for {app.friendly}")

        base_name = selected_mod.friendly if selected_mod is not None else f"{app.friendly}_mods"
        archive_path = await compress_mod_archive_entries(entries, base_name)
        delivery = await Distils.send_files(
            req.interaction.app.rest,
            channel_id,
            [archive_path],
            display_name=base_name,
        )
        if selected_mod is not None:
            status = (
                f"Sent `{selected_mod.friendly}` in a separate message."
                if delivery is not FileDeliveryMode.DIRECT
                else f"Posted direct download for `{selected_mod.friendly}` in a separate message."
            )
        else:
            status = (
                f"Sent mod download for `{app.friendly}` in a separate message."
                if delivery is not FileDeliveryMode.DIRECT
                else f"Posted direct mod download for `{app.friendly}` in a separate message."
            )
    except Exception as xcp:
        label = selected_mod.friendly if selected_mod is not None else app.friendly
        status = _error_status(f"Error: mod download failed for `{label}`: {xcp}")

    await host._edit_editor_message(
        interaction=req.interaction,
        actor_user_id=int(req.user_id),
        locale=req.locale,
        acl=acl,
        manager=manager,
        state=state,
        status=status,
    )


async def handle_mod_web_action(
    host: AppManageActionHost,
    *,
    req: EditorRequest,
    acl: Access_Control,
    manager: App_Manager,
    state: AppManageState,
    app: App,
) -> None:
    await req.interaction.create_initial_response(hikari.ResponseType.DEFERRED_MESSAGE_UPDATE)
    try:
        if reason := host.manage_lock_reason(app, message_id=host._interaction_message_id(req.interaction)):
            raise RuntimeError(reason)
        page_url = _current_node_app_url(app.name)
        status = f"Opened mod web page for `{app.friendly}`.\n{page_url}"
    except Exception as xcp:
        status = _error_status(f"Error: mod web failed for `{app.friendly}`: {xcp}")

    await host._edit_editor_message(
        interaction=req.interaction,
        actor_user_id=int(req.user_id),
        locale=req.locale,
        acl=acl,
        manager=manager,
        state=state,
        status=status,
    )


async def handle_app_update_action(
    host: AppManageActionHost,
    *,
    req: EditorRequest,
    acl: Access_Control,
    manager: App_Manager,
    state: AppManageState,
    app: App,
) -> None:
    await req.interaction.create_initial_response(hikari.ResponseType.DEFERRED_MESSAGE_UPDATE)
    try:
        if reason := host.manage_lock_reason(app, message_id=host._interaction_message_id(req.interaction)):
            raise RuntimeError(reason)
        if app.updater is None:
            raise _errors.UnsupportedUpdate(f"{app.friendly} does not have an updater")
        result = await app.updater.update_selected()
        status = result.message
    except Exception as xcp:
        status = _error_status(f"Error: update failed for `{app.friendly}`: {xcp}")

    await host._edit_editor_message(
        interaction=req.interaction,
        actor_user_id=int(req.user_id),
        locale=req.locale,
        acl=acl,
        manager=manager,
        state=state,
        status=status,
    )


async def consume_mod_upload(
    host: AppManageActionHost,
    *,
    upload: EditorFileUpload,
    bot: hikari.GatewayBot,
    acl: Access_Control,
    manager: App_Manager,
) -> None:
    meta = _mod_upload_meta_from_mapping(upload.request.meta)
    if meta is None:
        await bot.rest.create_message(upload.message.channel_id, "Error: upload request metadata is invalid.")
        return
    try:
        app = manager.get(meta.app_name)
    except ValueError as xcp:
        await edit_upload_response(
            host,
            bot=bot,
            acl=acl,
            manager=manager,
            meta=meta,
            channel_id=upload.message.channel_id,
            status=_error_status(f"Error: upload failed for `{meta.app_name}`: {xcp}"),
        )
        return

    base_state = AppManageState(
        mode=AppManageMode.MODS,
        page=meta.page,
        app_name=app.name,
        selected_page_slot=meta.selected_mod_slot,
    )
    try:
        reason = host.manage_lock_reason(app, message_id=upload.request.scope_id)
        if reason is not None:
            raise RuntimeError(reason)
        installed = await install_uploaded_mod(app=app, attachment=upload.attachment)
        next_state = host._state_for_mod(app=app, mod_name=installed.name, fallback=base_state)
        status = f"Installed `{installed.friendly}` to {app.friendly}."
    except Exception as xcp:
        next_state = base_state
        status = _error_status(f"Error: upload failed for `{app.friendly}`: {xcp}")

    await edit_upload_response(
        host,
        bot=bot,
        acl=acl,
        manager=manager,
        meta=meta,
        editor_message_id=upload.request.scope_id,
        actor_user_id=upload.request.user_id,
        channel_id=upload.message.channel_id,
        app=app,
        state=next_state,
        status=status,
    )


async def install_uploaded_mod(
    *,
    app: App,
    attachment: hikari.Attachment,
) -> Mod:
    if app.mods is None:
        raise _errors.UnsupportedModManager(app.friendly)
    require_app_stopped_for_mod_mutation(app)
    mod_names_before = {mod.name for mod in app.has_mod_manager.list_mods()}
    installed = await install_attachments(app.has_mod_manager, (attachment,), atomic=True)
    installed_mod = installed[0]
    if installed_mod.name not in mod_names_before:
        return installed_mod
    return app.has_mod_manager.get(installed_mod.name)


async def edit_upload_response(
    host: AppManageActionHost,
    *,
    bot: hikari.GatewayBot,
    acl: Access_Control,
    manager: App_Manager,
    meta: ModUploadRequestMeta,
    channel_id: hikari.Snowflakeish,
    status: EditorStatus | str,
    editor_message_id: hikari.Snowflake | None = None,
    actor_user_id: hikari.Snowflakeish | None = None,
    app: App | None = None,
    state: AppManageState | None = None,
) -> None:
    resolved_status = _coerce_status(status)
    status_text = _status_text(resolved_status)
    assert status_text is not None
    if app is not None and state is not None and editor_message_id is not None and actor_user_id is not None:
        host._touch_app_lock(message_id=editor_message_id, user_id=actor_user_id, app_name=app.name)
        host._extend_editor_session(editor_message_id)
        embed, components = host._render_editor(
            actor_user_id=int(actor_user_id),
            locale=meta.locale,
            acl=acl,
            manager=manager,
            state=state,
            status=resolved_status,
        )
        try:
            await bot.rest.edit_interaction_response(
                meta.application_id,
                meta.interaction_token,
                content=status_text,
                components=components,
                embeds=[] if embed is None else [embed],
            )
            return
        except Exception:
            log.exception("App.Manage.UploadEdit")
    await bot.rest.create_message(channel_id, status_text)


async def force_invalidate_lock(
    host: AppManageActionHost,
    *,
    bot: hikari.GatewayBot,
    lock: AppManagementLock,
    actor_user_id: int,
) -> bool:
    host._clear_pending_upload_request(channel_id=lock.channel_id, user_id=lock.user_id)
    host._release_app_lock(message_id=lock.message_id)
    if not lock.can_force_close(now=host._now()):
        return False
    application_id = lock.application_id
    interaction_token = lock.interaction_token
    if application_id is None or interaction_token is None:
        return False
    try:
        await bot.rest.edit_interaction_response(
            application_id,
            interaction_token,
            content=(
                f"App manager invalidated by <@{actor_user_id}>. Open `/app manage` again if you still need it."
            ),
            components=[],
            embeds=[],
        )
    except Exception:
        log.exception("App.Manage.ForceInvalidate")
        return False
    return True

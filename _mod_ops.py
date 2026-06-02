from __future__ import annotations

import asyncio
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

import hikari

from _file import File_Utils
from _security import Access_Control
from apps._app import App
from apps._config import ModDownloadBlockReason
from apps._mod import Mod, Mod_Manager


@dataclass(frozen=True, slots=True)
class ModMutationResult:
    successful: tuple[Mod, ...]
    errors: tuple[str, ...]


class NonDownloadableModError(RuntimeError):
    def __init__(self, mod: Mod) -> None:
        reason = mod.download_block_label or "not downloadable"
        super().__init__(f"{mod.friendly} is not downloadable ({reason}).")


class RunningAppModMutationError(RuntimeError):
    def __init__(self, app: App) -> None:
        super().__init__(f"{app.friendly} is running; stop it before changing mods.")


def require_app_stopped_for_mod_mutation(app: App) -> None:
    if app.check_running():
        raise RunningAppModMutationError(app)


async def install_attachments(
    manager: Mod_Manager,
    attachments: Collection[hikari.Attachment],
    *,
    atomic: bool,
) -> tuple[Mod, ...]:
    ordered_attachments = tuple(sorted(attachments, key=lambda attachment: attachment.filename.casefold()))
    download_paths = await asyncio.gather(*(File_Utils.download_temp(attachment) for attachment in ordered_attachments))
    installed: list[Mod] = []
    for path in download_paths:
        installed.append(await manager.add(path, atomic=atomic))
    return tuple(installed)


async def refresh_mod_index(manager: Mod_Manager) -> tuple[Mod, ...]:
    await manager.reload_mods()
    return tuple(manager.list_mods())


def download_paths(
    manager: Mod_Manager,
    mod_names: Collection[str] | None = None,
    *,
    default_enabled_only: bool,
) -> tuple[Path, ...]:
    if mod_names is not None:
        resolved_mods = [manager.get(mod_name) for mod_name in mod_names]
        for mod in resolved_mods:
            require_downloadable(mod)
    else:
        resolved_mods = manager.list_mods(True if default_enabled_only else None)
        resolved_mods = [mod for mod in resolved_mods if mod.downloadable]
    return tuple(mod.path for mod in resolved_mods if mod.path.exists())


def require_downloadable(mod: Mod) -> None:
    if not mod.downloadable:
        raise NonDownloadableModError(mod)


async def toggle_mod(
    manager: Mod_Manager,
    mod_name: str,
    *,
    acl: Access_Control,
    actor_user_id: int,
) -> Mod:
    mod = manager.get(mod_name)
    override_coremod = await _require_coremod_override(acl=acl, actor_user_id=actor_user_id, mod=mod)
    return await manager.toggle(mod, override_coremod=override_coremod)


async def remove_mods(
    manager: Mod_Manager,
    mod_names: Collection[str],
    *,
    acl: Access_Control,
    actor_user_id: int,
) -> ModMutationResult:
    removed: list[Mod] = []
    errors: list[str] = []
    for mod_name in mod_names:
        try:
            mod = manager.get(mod_name)
            override_coremod = await _require_coremod_override(acl=acl, actor_user_id=actor_user_id, mod=mod)
            removed.append(await manager.remove(mod, override_coremod=override_coremod))
        except Exception as xcp:
            errors.append(f"{xcp}: {mod_name}" if mod_name not in str(xcp) else str(xcp))
    return ModMutationResult(successful=tuple(removed), errors=tuple(errors))


async def toggle_coremod(
    manager: Mod_Manager,
    mod_name: str,
    *,
    acl: Access_Control,
    actor_user_id: int,
) -> Mod:
    await acl.perm_check(actor_user_id, acl.LvL.sudo)
    mod = manager.get(mod_name)
    if mod.is_builtin:
        raise RuntimeError("Built-in mods cannot be converted to or from coremods.")
    return await manager.set_coremod(mod, not mod.is_coremod_type)


async def toggle_downloadable(
    manager: Mod_Manager,
    mod_name: str,
    *,
    acl: Access_Control,
    actor_user_id: int,
    blocked_reason: ModDownloadBlockReason = ModDownloadBlockReason.SERVER_ONLY,
) -> Mod:
    await acl.perm_check(actor_user_id, acl.LvL.sudo)
    mod = manager.get(mod_name)
    reason = blocked_reason if mod.downloadable else mod.default_download_block_reason()
    return await manager.set_download_block_reason(mod, reason)


async def _require_coremod_override(
    *,
    acl: Access_Control,
    actor_user_id: int,
    mod: Mod,
) -> bool:
    if not mod.is_protected:
        return False
    await acl.perm_check(actor_user_id, acl.LvL.sudo)
    return True

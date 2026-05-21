from __future__ import annotations

import asyncio
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

import hikari

from _file import File_Utils
from _security import Access_Control
from apps._mod import Mod, Mod_Manager


@dataclass(frozen=True, slots=True)
class ModMutationResult:
    successful: tuple[Mod, ...]
    errors: tuple[str, ...]


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
    if mod_names:
        resolved_mods = [manager.get(mod_name) for mod_name in mod_names]
    else:
        resolved_mods = manager.list_mods(True if default_enabled_only else None)
    return tuple(mod.path for mod in resolved_mods if mod.path.exists())


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
    return await manager.set_coremod(mod, not mod.cfg.coremod)


async def _require_coremod_override(
    *,
    acl: Access_Control,
    actor_user_id: int,
    mod: Mod,
) -> bool:
    if not mod.cfg.coremod:
        return False
    await acl.perm_check(actor_user_id, acl.LvL.sudo)
    return True

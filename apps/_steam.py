"""SteamCMD app metadata discovery shared by Steam-backed apps."""

from __future__ import annotations

import asyncio
import importlib
import logging
import re
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypeGuard, cast

from apps._config import SteamUpdateBranch, SteamUpdateConfig, SteamUpdatePreset

log = logging.getLogger(__name__)

STEAM_BRANCH_CACHE_TTL_SECONDS: Final[float] = 12 * 60 * 60
_STEAM_APP_INFO_TIMEOUT_SECONDS: Final[float] = 30.0


@dataclass(frozen=True, slots=True)
class _SteamBranchCacheEntry:
    branches: tuple[SteamUpdateBranch, ...]
    captured_at_seconds: float


_STEAM_BRANCH_CACHE_LOCK = threading.RLock()
_STEAM_BRANCH_CACHE: dict[int, _SteamBranchCacheEntry] = {}
_STEAM_BRANCH_FETCHES: dict[int, asyncio.Task[tuple[SteamUpdateBranch, ...]]] = {}


def steam_update_preset_for_scope(scope: str | None) -> SteamUpdatePreset | None:
    """Return the SteamCMD preset exported by the app module for *scope*."""

    if scope is None:
        return None
    scope_key = scope.strip().casefold()
    if not scope_key or not scope_key.isidentifier():
        return None

    module_name = f"apps.{scope_key}"
    try:
        app_module = importlib.import_module(module_name)
    except ModuleNotFoundError as xcp:
        if xcp.name == module_name:
            return None
        raise

    preset = getattr(app_module, "STEAM_UPDATE_PRESET", None)
    if preset is None:
        return None
    if not isinstance(preset, SteamUpdatePreset):
        raise TypeError(
            f"{module_name}.STEAM_UPDATE_PRESET must be a SteamUpdatePreset."
        )
    return preset


def cached_steam_update_branches(
    app_id: int,
    *,
    allow_stale: bool = False,
) -> tuple[SteamUpdateBranch, ...] | None:
    """Return cached Steam branches for an app, if they are still usable."""

    with _STEAM_BRANCH_CACHE_LOCK:
        entry = _STEAM_BRANCH_CACHE.get(app_id)
        if entry is None:
            return None
        if (
            not allow_stale
            and time.monotonic() - entry.captured_at_seconds
            >= STEAM_BRANCH_CACHE_TTL_SECONDS
        ):
            return None
        return _copy_branches(entry.branches)


def steam_update_branch_cache_is_fresh(app_id: int) -> bool:
    """Return whether a Steam branch listing is cached within its 12-hour lifetime."""

    with _STEAM_BRANCH_CACHE_LOCK:
        entry = _STEAM_BRANCH_CACHE.get(app_id)
        return (
            entry is not None
            and time.monotonic() - entry.captured_at_seconds
            < STEAM_BRANCH_CACHE_TTL_SECONDS
        )


async def load_steam_update_branches(
    steam_update: SteamUpdateConfig,
    *,
    command_prefix: tuple[str, ...],
    working_directory: Path,
) -> tuple[SteamUpdateBranch, ...]:
    """Load an app's Steam branches, sharing a fresh result for twelve hours."""

    cached = cached_steam_update_branches(steam_update.app_id)
    if cached is not None:
        return cached

    loop = asyncio.get_running_loop()
    with _STEAM_BRANCH_CACHE_LOCK:
        cached = cached_steam_update_branches(steam_update.app_id)
        if cached is not None:
            return cached
        task = _STEAM_BRANCH_FETCHES.get(steam_update.app_id)
        if task is None or task.done() or task.get_loop() is not loop:
            task = loop.create_task(
                _fetch_steam_update_branches(
                    steam_update=steam_update,
                    command_prefix=command_prefix,
                    working_directory=working_directory,
                ),
                name=f"steam-branch-discovery-{steam_update.app_id}",
            )
            _STEAM_BRANCH_FETCHES[steam_update.app_id] = task
            task.add_done_callback(
                lambda completed_task, app_id=steam_update.app_id: _discard_completed_steam_branch_fetch(
                    app_id=app_id,
                    task=completed_task,
                )
            )

    return await asyncio.shield(task)


def merge_steam_update_branches(
    discovered_branches: Iterable[SteamUpdateBranch],
    configured_branches: Iterable[SteamUpdateBranch],
) -> tuple[SteamUpdateBranch, ...]:
    """Merge live Steam branches with local labels and beta-password overrides."""

    configured_branch_list = tuple(configured_branches)
    configured_by_key = {
        branch.branch_id.casefold(): branch for branch in configured_branch_list
    }
    merged_branches: list[SteamUpdateBranch] = []
    discovered_keys: set[str] = set()
    for discovered_branch in discovered_branches:
        branch_key = discovered_branch.branch_id.casefold()
        configured_branch = configured_by_key.get(branch_key)
        if configured_branch is None:
            merged_branches.append(discovered_branch.model_copy(deep=True))
        else:
            merged_branches.append(
                discovered_branch.model_copy(
                    update={
                        "label": configured_branch.label or discovered_branch.label,
                        "beta_password": configured_branch.beta_password,
                    }
                )
            )
        discovered_keys.add(branch_key)
    for configured_branch in configured_branch_list:
        if configured_branch.branch_id.casefold() not in discovered_keys:
            merged_branches.append(configured_branch.model_copy(deep=True))
    return tuple(merged_branches)


def build_steamcmd_login_arguments(steam_update: SteamUpdateConfig) -> tuple[str, ...]:
    """Return the SteamCMD login arguments for a configured account."""

    login = steam_update.login
    if login.username.casefold() == "anonymous":
        return ("+login", login.username)
    if login.password is None:
        raise ValueError("Steam login password is required for non-anonymous logins.")
    return ("+login", login.username, login.password)


def build_steamcmd_app_info_command(
    *,
    steam_update: SteamUpdateConfig,
    command_prefix: tuple[str, ...],
) -> list[str]:
    """Build the SteamCMD command used to discover an app's release branches."""

    command = [*command_prefix, *build_steamcmd_login_arguments(steam_update)]
    command.extend(
        ["+app_info_update", "1", "+app_info_print", str(steam_update.app_id), "+quit"]
    )
    return command


async def _fetch_steam_update_branches(
    *,
    steam_update: SteamUpdateConfig,
    command_prefix: tuple[str, ...],
    working_directory: Path,
) -> tuple[SteamUpdateBranch, ...]:
    working_directory.mkdir(parents=True, exist_ok=True)
    command = build_steamcmd_app_info_command(
        steam_update=steam_update,
        command_prefix=command_prefix,
    )
    log.info(
        "Discovering Steam branches: app_id=%s cwd=%s",
        steam_update.app_id,
        working_directory,
    )
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(working_directory),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=_STEAM_APP_INFO_TIMEOUT_SECONDS
        )
    except BaseException as xcp:
        if process.returncode is None:
            try:
                process.terminate()
                await process.wait()
            except ProcessLookupError:
                pass
        if isinstance(xcp, TimeoutError):
            raise RuntimeError(
                f"SteamCMD branch discovery timed out for app {steam_update.app_id}."
            ) from xcp
        raise

    output = _decode_steamcmd_output(stdout) + "\n" + _decode_steamcmd_output(stderr)
    if process.returncode != 0:
        raise RuntimeError(
            f"SteamCMD branch discovery failed for app {steam_update.app_id} with exit code {process.returncode}."
        )
    branches = parse_steam_app_info_branches(output, app_id=steam_update.app_id)
    with _STEAM_BRANCH_CACHE_LOCK:
        _STEAM_BRANCH_CACHE[steam_update.app_id] = _SteamBranchCacheEntry(
            branches=_copy_branches(branches),
            captured_at_seconds=time.monotonic(),
        )
    log.info(
        "Discovered Steam branches: app_id=%s count=%s",
        steam_update.app_id,
        len(branches),
    )
    return _copy_branches(branches)


def parse_steam_app_info_branches(
    text: str, *, app_id: int
) -> tuple[SteamUpdateBranch, ...]:
    """Parse the ``depots.branches`` table printed by ``steamcmd +app_info_print``."""

    app_info_mapping = parse_steam_keyvalues_mapping(
        _extract_steam_app_info_vdf(text, app_id=app_id),
        source_label=f"Steam app info for {app_id}",
    )
    raw_app_info = _required_string_key_mapping(
        app_info_mapping.get(str(app_id)),
        error=f"Steam app info for {app_id} does not contain its root mapping.",
    )
    raw_depots = _required_string_key_mapping(
        raw_app_info.get("depots"),
        error=f"Steam app info for {app_id} does not contain depots.",
    )
    raw_branches = _required_string_key_mapping(
        raw_depots.get("branches"),
        error=f"Steam app info for {app_id} does not contain depot branches.",
    )

    branches: list[SteamUpdateBranch] = []
    for raw_branch_id, raw_branch in raw_branches.items():
        raw_branch = _required_string_key_mapping(
            raw_branch,
            error=f"Steam app info branch {raw_branch_id!r} is invalid.",
        )
        raw_description = raw_branch.get("description")
        if raw_description is not None and not isinstance(raw_description, str):
            raise ValueError(
                f"Steam app info branch {raw_branch_id!r} has an invalid description."
            )
        label = (
            raw_description.strip()
            if isinstance(raw_description, str) and raw_description.strip()
            else None
        )
        branches.append(SteamUpdateBranch(branch_id=raw_branch_id, label=label))
    if not branches:
        raise ValueError(
            f"Steam app info for {app_id} does not list any depot branches."
        )
    return tuple(branches)


def parse_steam_keyvalues_mapping(text: str, *, source_label: str) -> dict[str, object]:
    """Parse a quoted Steam KeyValues document into a nested mapping."""

    tokens = _steam_keyvalues_tokens(text, source_label=source_label)
    payload, index = _parse_steam_keyvalues_block(
        tokens=tokens,
        index=0,
        stop_on_brace=False,
        source_label=source_label,
    )
    if index != len(tokens):
        raise ValueError(f"{source_label} has trailing tokens.")
    return payload


def _required_string_key_mapping(value: object, *, error: str) -> Mapping[str, object]:
    if not _is_string_key_mapping(value):
        raise ValueError(error)
    return value


def _is_string_key_mapping(value: object) -> TypeGuard[Mapping[str, object]]:
    if not isinstance(value, Mapping):
        return False
    mapping = cast(Mapping[object, object], value)
    return all(isinstance(key, str) for key in mapping)


def _extract_steam_app_info_vdf(text: str, *, app_id: int) -> str:
    root_match = re.search(rf'"{re.escape(str(app_id))}"\s*\{{', text)
    if root_match is None:
        raise ValueError(f"SteamCMD did not print app info for {app_id}.")
    opening_brace_index = text.find("{", root_match.start(), root_match.end())
    depth = 0
    in_string = False
    escaped = False
    for index in range(opening_brace_index, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[root_match.start() : index + 1]
    raise ValueError(f"Steam app info for {app_id} is missing a closing brace.")


def _steam_keyvalues_tokens(text: str, *, source_label: str) -> tuple[str, ...]:
    tokens: list[str] = []
    position = 0
    length = len(text)
    while position < length:
        char = text[position]
        if char.isspace():
            position += 1
            continue
        if text.startswith("//", position):
            newline_index = text.find("\n", position + 2)
            position = length if newline_index == -1 else newline_index + 1
            continue
        if char in "{}":
            tokens.append(char)
            position += 1
            continue
        if char != '"':
            raise ValueError(
                f"{source_label} contains an unexpected token near position {position}."
            )
        position += 1
        value_chars: list[str] = []
        while position < length:
            next_char = text[position]
            if next_char == "\\":
                if position + 1 >= length:
                    raise ValueError(
                        f"{source_label} contains an incomplete escape sequence."
                    )
                value_chars.append(text[position + 1])
                position += 2
                continue
            if next_char == '"':
                position += 1
                break
            value_chars.append(next_char)
            position += 1
        else:
            raise ValueError(f"{source_label} contains an unterminated string.")
        tokens.append("".join(value_chars))
    return tuple(tokens)


def _parse_steam_keyvalues_block(
    *,
    tokens: tuple[str, ...],
    index: int,
    stop_on_brace: bool,
    source_label: str,
) -> tuple[dict[str, object], int]:
    payload: dict[str, object] = {}
    while index < len(tokens):
        token = tokens[index]
        if token == "}":
            if not stop_on_brace:
                raise ValueError(
                    f"{source_label} contains an unexpected closing brace."
                )
            return payload, index + 1
        if token == "{":
            raise ValueError(f"{source_label} contains an unexpected opening brace.")
        key = token
        index += 1
        if index >= len(tokens):
            raise ValueError(f"{source_label} is missing a value for {key!r}.")
        next_token = tokens[index]
        if next_token == "{":
            value, index = _parse_steam_keyvalues_block(
                tokens=tokens,
                index=index + 1,
                stop_on_brace=True,
                source_label=source_label,
            )
            payload[key] = value
            continue
        if next_token == "}":
            raise ValueError(f"{source_label} is missing a value for {key!r}.")
        payload[key] = next_token
        index += 1
    if stop_on_brace:
        raise ValueError(f"{source_label} is missing a closing brace.")
    return payload, index


def _decode_steamcmd_output(value: bytes | None) -> str:
    return "" if value is None else value.decode(encoding="utf-8", errors="replace")


def _copy_branches(
    branches: Iterable[SteamUpdateBranch],
) -> tuple[SteamUpdateBranch, ...]:
    return tuple(branch.model_copy(deep=True) for branch in branches)


def _discard_completed_steam_branch_fetch(
    *,
    app_id: int,
    task: asyncio.Future[tuple[SteamUpdateBranch, ...]],
) -> None:
    with _STEAM_BRANCH_CACHE_LOCK:
        if _STEAM_BRANCH_FETCHES.get(app_id) is task:
            _STEAM_BRANCH_FETCHES.pop(app_id, None)

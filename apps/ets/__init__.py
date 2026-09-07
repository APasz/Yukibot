"""Euro Truck Simulator 2 dedicated-server integration."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import ClassVar, Final

import apps._scs_truck_simulator as _scs
from apps._config import AppVersion, SteamUpdatePreset

ETS_DEFAULT_CONNECTION_PORT: Final[int] = _scs.SCS_DEFAULT_CONNECTION_PORT
STEAM_GAME_APP_ID: Final[int] = _scs.ETS2_PROFILE.steam_game_app_id
STEAM_APP_ID: Final[int] = _scs.ETS2_PROFILE.steam_dedicated_server_app_id
STEAM_UPDATE_PRESET: Final[SteamUpdatePreset] = _scs.scs_steam_update_preset(_scs.ETS2_PROFILE)


def ets_data_home(directory: Path) -> Path:
    return _scs.scs_data_home(directory)


def ets_server_home(directory: Path) -> Path:
    return _scs.scs_server_home(directory, profile=_scs.ETS2_PROFILE)


def ets_server_config_path(directory: Path) -> Path:
    return _scs.scs_server_config_path(directory, profile=_scs.ETS2_PROFILE)


def ets_server_log_path(directory: Path) -> Path:
    return _scs.scs_server_log_path(directory, profile=_scs.ETS2_PROFILE)


def ets_server_package_paths(directory: Path) -> tuple[Path, Path]:
    return _scs.scs_server_package_paths(directory, profile=_scs.ETS2_PROFILE)


def ets_launch_environment(directory: Path) -> dict[str, str]:
    return _scs.scs_launch_environment(directory)


def resolve_ets_connection_port(port: int | None) -> int:
    return _scs.resolve_scs_connection_port(profile=_scs.ETS2_PROFILE, port=port)


def missing_ets_server_package_names(directory: Path) -> tuple[str, ...]:
    return _scs.missing_scs_server_package_names(directory=directory, profile=_scs.ETS2_PROFILE)


def configure_ets_server_ports(*, config_path: Path, connection_port: int | None) -> None:
    _scs.configure_scs_server_ports(
        config_path=config_path,
        connection_port=connection_port,
        profile=_scs.ETS2_PROFILE,
    )


async def prepare_ets_server_installation(*, directory: Path, connection_port: int | None) -> None:
    await _scs.prepare_scs_server_installation(
        directory=directory,
        connection_port=connection_port,
        profile=_scs.ETS2_PROFILE,
    )


def detect_ets_version(*, directory: Path, server_log: Path | None) -> AppVersion | None:
    return _scs.detect_scs_version(directory=directory, server_log=server_log, profile=_scs.ETS2_PROFILE)


class Mod_ETS(_scs.ScsTruckSimulatorMod):
    """ETS2's shared SCS mod implementation."""


class ETS_Settings(_scs.ScsTruckSimulatorSettings):
    def __init__(self, pointer: Path, *, version_getter: Callable[[], AppVersion | None] | None = None) -> None:
        super().__init__(pointer, profile=_scs.ETS2_PROFILE, version_getter=version_getter)


class ETS(_scs.ScsTruckSimulator):
    profile: ClassVar[_scs.ScsTruckSimulatorProfile] = _scs.ETS2_PROFILE


Matchers = _scs.ScsTruckSimulatorMatchers

"""Presentation metadata for actions that users can pin to their dashboard plate."""

from __future__ import annotations

from dataclasses import dataclass

from .user_settings import ModWebUserPlateAction


@dataclass(frozen=True, slots=True)
class UserPlateActionSpec:
    """Stable label and icon for one selectable user-plate action."""

    action: ModWebUserPlateAction
    label: str
    icon: str


_USER_PLATE_ACTION_SPECS: tuple[UserPlateActionSpec, ...] = (
    UserPlateActionSpec(ModWebUserPlateAction.MIRRORS, "Mirrors", "cloud_sync"),
    UserPlateActionSpec(ModWebUserPlateAction.SETTINGS, "Settings", "settings"),
    UserPlateActionSpec(ModWebUserPlateAction.STANDARD_DRINKS, "Standard drinks", "local_bar"),
    UserPlateActionSpec(ModWebUserPlateAction.CURRENCY, "Currency", "currency_exchange"),
    UserPlateActionSpec(ModWebUserPlateAction.DISCORD_TIME, "Discord Time", "schedule"),
    UserPlateActionSpec(ModWebUserPlateAction.UNIT_CONVERTER, "Unit converter", "straighten"),
    UserPlateActionSpec(ModWebUserPlateAction.ALIASES, "Aliases", "badge"),
    UserPlateActionSpec(ModWebUserPlateAction.LOG_OUT, "Log out", "logout"),
)
_USER_PLATE_ACTION_SPEC_BY_ACTION: dict[ModWebUserPlateAction, UserPlateActionSpec] = {
    spec.action: spec for spec in _USER_PLATE_ACTION_SPECS
}


def user_plate_action_options() -> dict[str, str]:
    """Return the ordered choices for the user-plate multiselect."""
    return {spec.action.value: spec.label for spec in _USER_PLATE_ACTION_SPECS}


def user_plate_action_icons_by_label() -> dict[str, str]:
    """Return the icon for each user-plate option label."""
    return {spec.label: spec.icon for spec in _USER_PLATE_ACTION_SPECS}


def user_plate_action_spec(action: ModWebUserPlateAction) -> UserPlateActionSpec:
    """Return display metadata for a supported pinned action."""
    return _USER_PLATE_ACTION_SPEC_BY_ACTION[action]

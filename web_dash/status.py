"""Composed status and account UI helpers for the mod-web dashboard."""

from __future__ import annotations

from .status_aliases import ModWebStatusAliasesMixin
from .status_appearance import ModWebUserAppearanceMixin
from .status_chat_preview import ModWebStatusChatPreviewMixin
from .status_currency_converter import ModWebStatusCurrencyConverterMixin
from .status_notifications import ModWebStatusNotificationsMixin
from .status_pages import ModWebStatusPagesMixin
from .status_standard_drinks import ModWebStatusStandardDrinksMixin
from .status_timestamp import ModWebStatusTimestampMixin
from .status_unit_converter import ModWebStatusUnitConverterMixin
from .status_user_settings import ModWebStatusUserSettingsMixin
from .status_utility_launcher import ModWebStatusUtilityLauncherMixin


class ModWebStatusMixin(
    ModWebStatusPagesMixin,
    ModWebStatusAliasesMixin,
    ModWebStatusUtilityLauncherMixin,
    ModWebStatusUserSettingsMixin,
    ModWebStatusStandardDrinksMixin,
    ModWebStatusCurrencyConverterMixin,
    ModWebStatusTimestampMixin,
    ModWebStatusUnitConverterMixin,
    ModWebStatusNotificationsMixin,
    ModWebStatusChatPreviewMixin,
    ModWebUserAppearanceMixin,
):
    """Aggregate feature-specific status UI mixins without changing service callers."""

    pass

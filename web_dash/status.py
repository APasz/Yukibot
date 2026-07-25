"""Composed status and account UI helpers for the mod-web dashboard."""

from __future__ import annotations

from currency_conversion import CurrencyConverter  # noqa: F401 - legacy patch/import surface

from .status_aliases import ModWebStatusAliasesMixin
from .status_appearance import ModWebUserAppearanceMixin
from .status_chat_preview import ModWebStatusChatPreviewMixin
from .status_notifications import ModWebStatusNotificationsMixin
from .status_pages import ModWebStatusPagesMixin
from .status_support import _status_svg_markup, config  # noqa: F401 - legacy patch/import surface
from .status_utilities import ModWebStatusUtilitiesMixin


class ModWebStatusMixin(
    ModWebStatusPagesMixin,
    ModWebStatusAliasesMixin,
    ModWebStatusUtilitiesMixin,
    ModWebStatusNotificationsMixin,
    ModWebStatusChatPreviewMixin,
    ModWebUserAppearanceMixin,
):
    """Aggregate feature-specific status UI mixins without changing service callers."""

    pass

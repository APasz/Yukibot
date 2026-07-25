"""Per-user appearance helpers for the mod-web dashboard.

This module owns palette resolution and the CSS bridge used to apply a saved
appearance to an already-rendered NiceGUI client.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from mod_web_theme import DEFAULT_MOD_WEB_THEME

from .nicegui_protocols import ModWebUi
from .runtime_imports import Mapping, ModWebUser, json
from .service_base import ModWebServiceSupport
from .user_settings import ModWebAppearanceSettings

_USER_ACCENT_STYLE_ELEMENT_ID = "mod-web-user-accent-style"
_UserAppearanceColorKey = Literal[
    "primary_color_hex",
    "positive_color_hex",
    "warning_color_hex",
    "negative_color_hex",
    "info_color_hex",
]


@dataclass(frozen=True, slots=True)
class _UserAppearanceColorSpec:
    key: _UserAppearanceColorKey
    label: str
    css_variables: tuple[str, ...]


_USER_APPEARANCE_COLOR_SPECS: tuple[_UserAppearanceColorSpec, ...] = (
    _UserAppearanceColorSpec(
        key="primary_color_hex",
        label="Accent colour",
        css_variables=("--mod-accent", "--q-primary", "--q-accent"),
    ),
    _UserAppearanceColorSpec(
        key="positive_color_hex",
        label="Positive colour",
        css_variables=("--mod-positive", "--q-positive"),
    ),
    _UserAppearanceColorSpec(
        key="warning_color_hex",
        label="Warning colour",
        css_variables=("--mod-warning", "--q-warning"),
    ),
    _UserAppearanceColorSpec(
        key="negative_color_hex",
        label="Error colour",
        css_variables=("--mod-red", "--mod-negative", "--q-negative"),
    ),
    _UserAppearanceColorSpec(
        key="info_color_hex",
        label="Info colour",
        css_variables=("--mod-info", "--q-info"),
    ),
)


class ModWebUserAppearanceMixin(ModWebServiceSupport):
    """Apply and transform persisted user-specific dashboard appearance."""

    def _apply_theme_for_user(self, *, ui: ModWebUi, user: ModWebUser) -> None:
        self._apply_theme(ui=ui)
        self._apply_user_appearance_palette(ui=ui, user=user)

    def _apply_user_appearance_palette(self, *, ui: ModWebUi, user: ModWebUser) -> None:
        settings = self._backend.user_settings_for(user_id=user.discord_id)
        colors = self._resolved_user_appearance_colors(settings.appearance)
        ui.colors(
            primary=colors["primary_color_hex"],
            secondary=DEFAULT_MOD_WEB_THEME.palette.nicegui.secondary,
            accent=colors["primary_color_hex"],
            positive=colors["positive_color_hex"],
            negative=colors["negative_color_hex"],
            info=colors["info_color_hex"],
            warning=colors["warning_color_hex"],
        )
        ui.add_head_html(self._user_appearance_style_html(settings.appearance))

    @staticmethod
    def _default_user_accent_color_hex() -> str:
        return DEFAULT_MOD_WEB_THEME.palette.purple.upper()

    @staticmethod
    def _default_user_positive_color_hex() -> str:
        return DEFAULT_MOD_WEB_THEME.palette.nicegui.positive.upper()

    @staticmethod
    def _default_user_warning_color_hex() -> str:
        return DEFAULT_MOD_WEB_THEME.palette.warning.upper()

    @staticmethod
    def _default_user_negative_color_hex() -> str:
        return DEFAULT_MOD_WEB_THEME.palette.red.upper()

    @classmethod
    def _default_user_appearance_colors(cls) -> dict[_UserAppearanceColorKey, str]:
        accent = cls._default_user_accent_color_hex()
        return {
            "primary_color_hex": accent,
            "positive_color_hex": cls._default_user_positive_color_hex(),
            "warning_color_hex": cls._default_user_warning_color_hex(),
            "negative_color_hex": cls._default_user_negative_color_hex(),
            "info_color_hex": accent,
        }

    @classmethod
    def _resolved_user_appearance_colors(
        cls,
        appearance: ModWebAppearanceSettings,
    ) -> dict[_UserAppearanceColorKey, str]:
        defaults = cls._default_user_appearance_colors()
        accent = appearance.primary_color_hex or defaults["primary_color_hex"]
        return {
            "primary_color_hex": accent,
            "positive_color_hex": appearance.positive_color_hex or defaults["positive_color_hex"],
            "warning_color_hex": appearance.warning_color_hex or defaults["warning_color_hex"],
            "negative_color_hex": appearance.negative_color_hex or defaults["negative_color_hex"],
            "info_color_hex": appearance.info_color_hex or accent,
        }

    @staticmethod
    def _appearance_color_rgb(color_hex: str) -> tuple[int, int, int]:
        normalized = ModWebAppearanceSettings(primary_color_hex=color_hex).primary_color_hex
        if normalized is None:
            raise ValueError("Appearance colour is required.")
        return (
            int(normalized[1:3], 16),
            int(normalized[3:5], 16),
            int(normalized[5:7], 16),
        )

    @classmethod
    def _appearance_color_mix(cls, color_hex: str, *, color_weight: float, other_hex: str) -> str:
        red, green, blue = cls._appearance_color_rgb(color_hex)
        other_red, other_green, other_blue = cls._appearance_color_rgb(other_hex)
        mixed = (
            round((red * color_weight) + (other_red * (1.0 - color_weight))),
            round((green * color_weight) + (other_green * (1.0 - color_weight))),
            round((blue * color_weight) + (other_blue * (1.0 - color_weight))),
        )
        return f"#{mixed[0]:02X}{mixed[1]:02X}{mixed[2]:02X}"

    @classmethod
    def _appearance_color_alpha(cls, color_hex: str, alpha: float) -> str:
        red, green, blue = cls._appearance_color_rgb(color_hex)
        return f"rgba({red}, {green}, {blue}, {alpha:.2f})"

    @classmethod
    def _user_appearance_derived_css_variables(
        cls,
        *,
        prefix: str,
        color_hex: str,
        dark_weight: float,
        surface_weight: float,
        surface_base_hex: str = "#050507",
        text_weight: float = 0.22,
    ) -> dict[str, str]:
        return {
            f"--mod-{prefix}-dark": cls._appearance_color_mix(color_hex, color_weight=dark_weight, other_hex="#050507"),
            f"--mod-{prefix}-surface": cls._appearance_color_mix(color_hex, color_weight=surface_weight, other_hex=surface_base_hex),
            f"--mod-{prefix}-text": cls._appearance_color_mix(color_hex, color_weight=text_weight, other_hex="#FFFFFF"),
            f"--mod-{prefix}-border": cls._appearance_color_alpha(color_hex, 0.58),
            f"--mod-{prefix}-border-strong": cls._appearance_color_alpha(color_hex, 0.74),
            f"--mod-{prefix}-glow": cls._appearance_color_alpha(color_hex, 0.24),
        }

    @classmethod
    def _user_appearance_css_variables(cls, appearance: ModWebAppearanceSettings) -> dict[str, str]:
        colors_by_key = cls._resolved_user_appearance_colors(appearance)
        variables: dict[str, str] = {}
        for spec in _USER_APPEARANCE_COLOR_SPECS:
            for variable_name in spec.css_variables:
                variables[variable_name] = colors_by_key[spec.key]
        accent_color_hex = colors_by_key["primary_color_hex"]
        variables.update(cls._user_appearance_derived_css_variables(prefix="accent", color_hex=accent_color_hex, dark_weight=0.38, surface_weight=0.22, text_weight=0.36))
        variables["--mod-accent-panel"] = cls._appearance_color_mix(accent_color_hex, color_weight=0.16, other_hex="#111118")
        variables["--mod-accent-text-strong"] = cls._appearance_color_mix(accent_color_hex, color_weight=0.18, other_hex="#FFFFFF")
        variables["--mod-accent-faint"] = cls._appearance_color_alpha(accent_color_hex, 0.12)
        variables["--mod-accent-wash"] = cls._appearance_color_alpha(accent_color_hex, 0.08)
        for prefix, color_hex, dark_weight, surface_weight, text_weight in (
            ("info", colors_by_key["info_color_hex"], 0.38, 0.22, 0.22),
            ("positive", colors_by_key["positive_color_hex"], 0.38, 0.22, 0.22),
            ("negative", colors_by_key["negative_color_hex"], 0.42, 0.22, 0.22),
            ("warning", colors_by_key["warning_color_hex"], 0.30, 0.18, 0.28),
        ):
            variables.update(cls._user_appearance_derived_css_variables(prefix=prefix, color_hex=color_hex, dark_weight=dark_weight, surface_weight=surface_weight, text_weight=text_weight))
        variables["--mod-red-dark"] = variables["--mod-negative-dark"]
        return variables

    @classmethod
    def _user_appearance_style_html(cls, appearance: ModWebAppearanceSettings) -> str:
        variables = cls._user_appearance_css_variables(appearance)
        declarations = " ".join(f"{variable_name}: {color_hex};" for variable_name, color_hex in sorted(variables.items()))
        return (
            f'<style id="{_USER_ACCENT_STYLE_ELEMENT_ID}">:root {{ {declarations} }}</style>'
            "<script>(() => {"
            f"const variables = {json.dumps(variables)};"
            "const root = document.documentElement;"
            f"{cls._appearance_css_variable_assignment_javascript()}"
            "})()</script>"
        )

    @staticmethod
    def _appearance_css_variable_assignment_javascript() -> str:
        return (
            "const targets = [root, document.body].filter(Boolean);"
            "for (const target of targets) {"
            "for (const [name, value] of Object.entries(variables)) { target.style.setProperty(name, value); }"
            "}"
        )

    @classmethod
    def _user_appearance_javascript(cls, variables: dict[str, str] | None) -> str:
        encoded_variables = json.dumps(variables)
        encoded_style_id = json.dumps(_USER_ACCENT_STYLE_ELEMENT_ID)
        encoded_variable_names = json.dumps(tuple(sorted(cls._user_appearance_css_variables(ModWebAppearanceSettings()).keys())))
        return (
            "(() => {"
            f"const variables = {encoded_variables};"
            f"const styleId = {encoded_style_id};"
            f"const variableNames = {encoded_variable_names};"
            "const root = document.documentElement;"
            "let style = document.getElementById(styleId);"
            "if (variables && typeof variables === 'object') {"
            "const declarations = Object.entries(variables)"
            ".map(([name, value]) => `${name}: ${value};`).join(' ');"
            "const css = `:root { ${declarations} }`;"
            "if (!style) { style = document.createElement('style'); style.id = styleId; document.head.appendChild(style); }"
            "style.textContent = css;"
            f"{cls._appearance_css_variable_assignment_javascript()}"
            "} else {"
            "if (style) { style.remove(); }"
            "const targets = [root, document.body].filter(Boolean);"
            "for (const target of targets) { for (const name of variableNames) { target.style.removeProperty(name); } }"
            "}"
            "})()"
        )

    @classmethod
    def _normalized_user_appearance_color_hex(cls, color_hex: str) -> str:
        return ModWebAppearanceSettings(primary_color_hex=color_hex).primary_color_hex or cls._default_user_accent_color_hex()

    @staticmethod
    def _user_appearance_with_colors(
        *,
        appearance: ModWebAppearanceSettings,
        colors_by_key: Mapping[_UserAppearanceColorKey, str | None],
    ) -> ModWebAppearanceSettings:
        return ModWebAppearanceSettings(
            color_scheme=appearance.color_scheme,
            primary_color_hex=colors_by_key["primary_color_hex"],
            positive_color_hex=colors_by_key["positive_color_hex"],
            warning_color_hex=colors_by_key["warning_color_hex"],
            negative_color_hex=colors_by_key["negative_color_hex"],
            info_color_hex=colors_by_key["info_color_hex"],
        )

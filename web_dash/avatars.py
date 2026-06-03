from __future__ import annotations

from .runtime_imports import Path, Power_Level, base64, escape, lru_cache

_USER_AVATAR_ICON_DIRECTORY: Path = Path(__file__).resolve().parent.parent / "resources" / "icon"
_USER_AVATAR_SVG_TEMPLATE_PATH: Path = (
    Path(__file__).resolve().parent.parent / "resources" / "svg" / "web_dash" / "user_avatar_fallback.svg"
)
_USER_AVATAR_ICON_PATH_BY_LEVEL: dict[Power_Level, Path] = {
    Power_Level.guest: _USER_AVATAR_ICON_DIRECTORY / "guest.jpg",
    Power_Level.visitor: _USER_AVATAR_ICON_DIRECTORY / "visitor.webp",
    Power_Level.user: _USER_AVATAR_ICON_DIRECTORY / "user.jpg",
    Power_Level.admin: _USER_AVATAR_ICON_DIRECTORY / "admin.jpg",
    Power_Level.sudo: _USER_AVATAR_ICON_DIRECTORY / "sudo.png",
    Power_Level.root: _USER_AVATAR_ICON_DIRECTORY / "root.jpg",
}
_USER_AVATAR_SVG_ACCENT_BY_LEVEL: dict[Power_Level, str] = {
    Power_Level.guest: "#71717a",
    Power_Level.visitor: "#a1a1aa",
    Power_Level.user: "#d4d4d8",
    Power_Level.admin: "#8b5cf6",
    Power_Level.sudo: "#dc2626",
    Power_Level.root: "#f59e0b",
}
_USER_AVATAR_SVG_BADGE_MARKUP_BY_LEVEL: dict[Power_Level, str] = {
    Power_Level.guest: (
        '<path d="M45 45.5c0-1.9 1.3-3.5 3-4.1 1.6-.7 2.7-2.2 2.7-4 0-2.5-2.1-4.4-4.7-4.2-2.2.1-4 1.8-4.3 4" '
        'fill="none" stroke="{accent}" stroke-width="2.2" stroke-linecap="square" stroke-linejoin="round"/>'
        '<circle cx="48" cy="50.8" r="1.5" fill="{accent}"/>'
    ),
    Power_Level.visitor: (
        '<path d="M39.5 48c2.4-4 5.8-6 8.5-6s6.1 2 8.5 6c-2.4 4-5.8 6-8.5 6s-6.1-2-8.5-6Z" '
        'fill="none" stroke="{accent}" stroke-width="2.1" stroke-linejoin="round"/>'
        '<circle cx="48" cy="48" r="2.2" fill="{accent}"/>'
    ),
    Power_Level.user: (
        '<path d="m42 48 4 4 8-8" fill="none" stroke="{accent}" stroke-width="2.4" '
        'stroke-linecap="square" stroke-linejoin="round"/>'
    ),
    Power_Level.admin: (
        '<path d="M48 40.5 54 43v4.5c0 4.1-2.6 7-6 8.8-3.4-1.8-6-4.7-6-8.8V43l6-2.5Z" '
        'fill="none" stroke="{accent}" stroke-width="2.2" stroke-linejoin="round"/>'
    ),
    Power_Level.sudo: (
        '<circle cx="45.5" cy="47.5" r="3.1" fill="none" stroke="{accent}" stroke-width="2.1"/>'
        '<path d="M48.5 47.5H56m-2 0v2.3m-2.4-2.3v2.3" fill="none" stroke="{accent}" stroke-width="2.1" '
        'stroke-linecap="square" stroke-linejoin="round"/>'
    ),
    Power_Level.root: (
        '<path d="m40.5 52 1.9-9.2 5.6 4.6 5.6-4.6L55.5 52Z" fill="none" stroke="{accent}" '
        'stroke-width="2.1" stroke-linejoin="round"/>'
        '<path d="M42.4 44.8 39.8 42m8.2 2-1.7-4.1m7.3 4.1 1.7-4.1" fill="none" stroke="{accent}" '
        'stroke-width="2.1" stroke-linecap="square"/>'
    ),
}
_USER_AVATAR_MIME_TYPE_BY_SUFFIX: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
}


@lru_cache(maxsize=None)
def _user_avatar_icon_data_uri(level: Power_Level) -> str | None:
    icon_path: Path = _USER_AVATAR_ICON_PATH_BY_LEVEL[level]
    mime_type: str | None = _USER_AVATAR_MIME_TYPE_BY_SUFFIX.get(icon_path.suffix.casefold())
    if mime_type is None:
        return None
    try:
        icon_bytes: bytes = icon_path.read_bytes()
    except OSError:
        return None
    encoded_icon: str = base64.b64encode(icon_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded_icon}"


def _user_avatar_fallback_svg_markup(level: Power_Level) -> str:
    accent_color_hex: str = _USER_AVATAR_SVG_ACCENT_BY_LEVEL[level]
    badge_markup: str = _USER_AVATAR_SVG_BADGE_MARKUP_BY_LEVEL[level].format(accent=accent_color_hex)
    aria_label: str = escape(f"{level.name.title()} avatar fallback", quote=True)
    return _user_avatar_fallback_svg_template().format(
        aria_label=aria_label,
        accent_color_hex=accent_color_hex,
        badge_markup=badge_markup,
    )


@lru_cache(maxsize=None)
def _user_avatar_fallback_svg_data_uri(level: Power_Level) -> str:
    svg_markup: str = _user_avatar_fallback_svg_markup(level)
    encoded_svg: str = base64.b64encode(svg_markup.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded_svg}"


@lru_cache(maxsize=1)
def _user_avatar_fallback_svg_template() -> str:
    return _USER_AVATAR_SVG_TEMPLATE_PATH.read_text(encoding="utf-8").strip()


__all__: tuple[str, ...] = (
    "_USER_AVATAR_ICON_DIRECTORY",
    "_USER_AVATAR_ICON_PATH_BY_LEVEL",
    "_USER_AVATAR_MIME_TYPE_BY_SUFFIX",
    "_USER_AVATAR_SVG_ACCENT_BY_LEVEL",
    "_USER_AVATAR_SVG_BADGE_MARKUP_BY_LEVEL",
    "_user_avatar_fallback_svg_template",
    "_user_avatar_fallback_svg_data_uri",
    "_user_avatar_fallback_svg_markup",
    "_user_avatar_icon_data_uri",
)

from __future__ import annotations

import base64
import enum
from functools import lru_cache
from pathlib import Path

from _security import Access_Control, Power_Level


class MinecraftDefaultSkin(enum.StrEnum):
    STEVE = "steve"
    ALEX = "alex"
    NOOR = "noor"
    SUNNY = "sunny"
    ARI = "ari"
    ZURI = "zuri"
    MAKENA = "makena"
    KAI = "kai"
    EFE = "efe"


_DEFAULT_SKIN_DIRECTORY = Path(__file__).resolve().parent / "resources" / "minecraft_default_skins"
_DEV_BYPASS_DEFAULT_SKIN_BY_LEVEL: dict[Power_Level, MinecraftDefaultSkin] = {
    Power_Level.guest: MinecraftDefaultSkin.SUNNY,
    Power_Level.visitor: MinecraftDefaultSkin.STEVE,
    Power_Level.user: MinecraftDefaultSkin.NOOR,
    Power_Level.admin: MinecraftDefaultSkin.KAI,
    Power_Level.sudo: MinecraftDefaultSkin.ARI,
    Power_Level.root: MinecraftDefaultSkin.ALEX,
}


def minecraft_default_skin_for_dev_bypass_user(user_id: int) -> MinecraftDefaultSkin | None:
    level = Access_Control.dev_bypass_level(user_id)
    if level is None:
        return None
    return _DEV_BYPASS_DEFAULT_SKIN_BY_LEVEL.get(level)


@lru_cache(maxsize=None)
def minecraft_default_skin_head_data_uri(skin: MinecraftDefaultSkin) -> str | None:
    skin_path = _DEFAULT_SKIN_DIRECTORY / f"{skin.value}.png"
    try:
        skin_png = skin_path.read_bytes()
    except OSError:
        return None

    skin_png_data_uri = f"data:image/png;base64,{base64.b64encode(skin_png).decode('ascii')}"
    svg_markup = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8" shape-rendering="crispEdges" '
        'style="image-rendering: pixelated;">'
        f'<image href="{skin_png_data_uri}" x="-8" y="-8" width="64" height="64"/>'
        f'<image href="{skin_png_data_uri}" x="-40" y="-8" width="64" height="64"/>'
        "</svg>"
    )
    encoded_svg = base64.b64encode(svg_markup.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded_svg}"


def minecraft_dev_bypass_head_data_uri(user_id: int) -> str | None:
    skin = minecraft_default_skin_for_dev_bypass_user(user_id)
    if skin is None:
        return None
    return minecraft_default_skin_head_data_uri(skin)

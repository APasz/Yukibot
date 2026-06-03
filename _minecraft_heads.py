from __future__ import annotations

import base64
import enum
import logging
import tempfile
from pathlib import Path

import requests

from _security import Access_Control, Power_Level

log = logging.getLogger(__name__)


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


_DEFAULT_SKIN_DIRECTORY: Path = Path(__file__).resolve().parent / "resources" / "minecraft_default_skins"
_DEFAULT_SKIN_HEAD_SVG_TEMPLATE_PATH: Path = Path(__file__).resolve().parent / "resources" / "svg" / "minecraft" / "default_skin_head.svg"
_DEFAULT_SKIN_DOWNLOAD_BASE_URL: str = (
    "https://assets.mcasset.cloud/latest/assets/minecraft/textures/entity/player/wide"
)
_DEFAULT_SKIN_DOWNLOAD_TIMEOUT_SECONDS: float = 10.0
_DEFAULT_SKIN_DIMENSIONS: tuple[int, int] = (64, 64)
_PNG_SIGNATURE: bytes = b"\x89PNG\r\n\x1a\n"
_DEV_BYPASS_DEFAULT_SKIN_BY_LEVEL: dict[Power_Level, MinecraftDefaultSkin] = {
    Power_Level.guest: MinecraftDefaultSkin.SUNNY,
    Power_Level.visitor: MinecraftDefaultSkin.STEVE,
    Power_Level.user: MinecraftDefaultSkin.NOOR,
    Power_Level.admin: MinecraftDefaultSkin.KAI,
    Power_Level.sudo: MinecraftDefaultSkin.ARI,
    Power_Level.root: MinecraftDefaultSkin.ALEX,
}


def minecraft_default_skin_for_dev_bypass_user(user_id: int) -> MinecraftDefaultSkin | None:
    level: Power_Level | None = Access_Control.dev_bypass_level(user_id)
    if level is None:
        return None
    return _DEV_BYPASS_DEFAULT_SKIN_BY_LEVEL.get(level)


def minecraft_default_skin_head_data_uri(skin: MinecraftDefaultSkin) -> str | None:
    cached: str | None = _HEAD_DATA_URI_CACHE.get(skin)
    if cached is not None and _default_skin_path(skin).is_file():
        return cached

    try:
        skin_png: bytes = _read_or_download_default_skin(skin)
    except (OSError, requests.RequestException, ValueError) as xcp:
        log.warning(
            "Minecraft default skin unavailable: skin=%s error=%s: %s",
            skin.value,
            type(xcp).__name__,
            xcp,
        )
        return None

    return _cached_minecraft_default_skin_head_data_uri(skin, skin_png)


_HEAD_DATA_URI_CACHE: dict[MinecraftDefaultSkin, str] = {}


def _cached_minecraft_default_skin_head_data_uri(skin: MinecraftDefaultSkin, skin_png: bytes) -> str:
    cached: str | None = _HEAD_DATA_URI_CACHE.get(skin)
    if cached is not None:
        return cached

    data_uri: str = _minecraft_head_data_uri_from_skin_png(skin_png)
    _HEAD_DATA_URI_CACHE[skin] = data_uri
    return data_uri


def _minecraft_head_data_uri_from_skin_png(skin_png: bytes) -> str:
    skin_png_data_uri: str = f"data:image/png;base64,{base64.b64encode(skin_png).decode('ascii')}"
    svg_markup: str = _default_skin_head_svg_template().format(skin_png_data_uri=skin_png_data_uri)
    encoded_svg: str = base64.b64encode(svg_markup.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded_svg}"


def _default_skin_head_svg_template() -> str:
    return _DEFAULT_SKIN_HEAD_SVG_TEMPLATE_PATH.read_text(encoding="utf-8").strip()


def _read_or_download_default_skin(skin: MinecraftDefaultSkin) -> bytes:
    skin_path: Path = _default_skin_path(skin)
    try:
        skin_png: bytes = skin_path.read_bytes()
    except FileNotFoundError:
        return _download_default_skin(skin=skin, skin_path=skin_path)

    _validate_default_skin_png(skin=skin, skin_png=skin_png, source=str(skin_path))
    return skin_png


def _download_default_skin(*, skin: MinecraftDefaultSkin, skin_path: Path) -> bytes:
    url: str = _default_skin_download_url(skin)
    response: requests.Response = requests.get(url, timeout=_DEFAULT_SKIN_DOWNLOAD_TIMEOUT_SECONDS)
    response.raise_for_status()

    skin_png: bytes = response.content
    _validate_default_skin_png(skin=skin, skin_png=skin_png, source=url)
    _write_default_skin(skin_path=skin_path, skin_png=skin_png)
    return skin_png


def _default_skin_path(skin: MinecraftDefaultSkin) -> Path:
    return _DEFAULT_SKIN_DIRECTORY / f"{skin.value}.png"


def _default_skin_download_url(skin: MinecraftDefaultSkin) -> str:
    return f"{_DEFAULT_SKIN_DOWNLOAD_BASE_URL}/{skin.value}.png"


def _write_default_skin(*, skin_path: Path, skin_png: bytes) -> None:
    skin_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=skin_path.parent,
            prefix=f".{skin_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(skin_png)
        temp_path.replace(skin_path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _validate_default_skin_png(*, skin: MinecraftDefaultSkin, skin_png: bytes, source: str) -> None:
    dimensions: tuple[int, int] | None = _png_dimensions(skin_png)
    if dimensions != _DEFAULT_SKIN_DIMENSIONS:
        raise ValueError(
            f"Minecraft default skin {skin.value} from {source} must be a "
            f"{_DEFAULT_SKIN_DIMENSIONS[0]}x{_DEFAULT_SKIN_DIMENSIONS[1]} PNG, got {dimensions}"
        )


def _png_dimensions(png: bytes) -> tuple[int, int] | None:
    if len(png) < 24 or not png.startswith(_PNG_SIGNATURE):
        return None
    if png[12:16] != b"IHDR":
        return None
    width: int = int.from_bytes(png[16:20], "big")
    height: int = int.from_bytes(png[20:24], "big")
    return width, height


def minecraft_dev_bypass_head_data_uri(user_id: int) -> str | None:
    skin: MinecraftDefaultSkin | None = minecraft_default_skin_for_dev_bypass_user(user_id)
    if skin is None:
        return None
    return minecraft_default_skin_head_data_uri(skin)

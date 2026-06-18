from __future__ import annotations

import asyncio
import hashlib
import logging
from os import PathLike
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Final, Literal, Protocol
from urllib.parse import parse_qsl, quote, urlsplit

import requests

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from fontTools.ttLib import TTFont as _FontToolsTTFont

FontFlavor = Literal["woff", "woff2"]

_FONT_SOURCE_SUFFIXES: Final[frozenset[str]] = frozenset({".otf", ".ttf"})
_GOOGLE_FONT_BLOCK_RE: Final[re.Pattern[str]] = re.compile(r"(?P<comment>/\*.*?\*/)?\s*@font-face\s*\{(?P<body>.*?)\}", re.DOTALL)
_GOOGLE_FONT_SRC_RE: Final[re.Pattern[str]] = re.compile(
    r"url\((?P<quote>['\"]?)(?P<url>https://[^)'\"]+)(?P=quote)\)\s*format\((?P<fmt_quote>['\"]?)(?P<fmt>woff2?|truetype|opentype)(?P=fmt_quote)\)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class FontAssetEntry:
    family_name: str
    scope: str | None
    source_path: Path
    woff_path: Path | None
    woff2_path: Path | None


@dataclass(frozen=True, slots=True)
class FontAssetRefreshResult:
    entries: tuple[FontAssetEntry, ...]
    converted_count: int


class _TTFontLike(Protocol):
    @property
    def flavor(self) -> str | None: ...

    @flavor.setter
    def flavor(self, value: str | None) -> None: ...

    def __getitem__(self, key: str) -> object: ...

    def save(self, file: str | PathLike[str] | BinaryIO, reorderTables: bool | None = True) -> None: ...

    def close(self) -> None: ...


class _TTFontAdapter:
    def __init__(self, font: _FontToolsTTFont) -> None:
        self._font = font

    @property
    def flavor(self) -> str | None:
        return self._font.flavor

    @flavor.setter
    def flavor(self, value: str | None) -> None:
        self._font.flavor = value

    def __getitem__(self, key: str) -> object:
        return self._font[key]

    def save(self, file: str | PathLike[str] | BinaryIO, reorderTables: bool | None = True) -> None:
        self._font.save(file, reorderTables=reorderTables)

    def close(self) -> None:
        self._font.close()


class FontAssetRegistry:
    def __init__(self, *, fonts_root: Path | None = None) -> None:
        self._fonts_root = Path("resources/fonts") if fonts_root is None else fonts_root
        self._entries: tuple[FontAssetEntry, ...] = ()
        self._refresh_task: asyncio.Task[None] | None = None
        self._refresh_pending = False
        self._google_font_urls: tuple[str, ...] = ()
        self._woff2_missing_logged = False

    @property
    def entries(self) -> tuple[FontAssetEntry, ...]:
        return self._entries

    @property
    def fonts_root(self) -> Path:
        return self._fonts_root

    def available_font_families(self, *, scope: str | None) -> tuple[str, ...]:
        matching_family_names: list[str] = []
        seen_family_names: set[str] = set()
        scope_key = None if scope is None else scope.strip().casefold()
        for entry in self._public_entries():
            if entry.scope is not None and scope_key != entry.scope.casefold():
                continue
            family_key = entry.family_name.casefold()
            if family_key in seen_family_names:
                continue
            seen_family_names.add(family_key)
            matching_family_names.append(entry.family_name)
        return tuple(sorted(matching_family_names, key=str.casefold))

    def font_face_css_html(self, *, base_path: str) -> str:
        css_blocks: list[str] = []
        for entry in self._public_entries():
            source_rules: list[str] = []
            if entry.woff2_path is not None:
                source_rules.append(f'url("{self._asset_url(base_path=base_path, asset_path=entry.woff2_path)}") format("woff2")')
            if entry.woff_path is not None:
                source_rules.append(f'url("{self._asset_url(base_path=base_path, asset_path=entry.woff_path)}") format("woff")')
            if not source_rules:
                continue
            css_blocks.append(
                "@font-face {"
                f'font-family: {self._css_string_literal(entry.family_name)};'
                f"src: {', '.join(source_rules)};"
                "font-display: swap;"
                "}"
            )
        if not css_blocks:
            return ""
        return f"<style>{''.join(css_blocks)}</style>"

    def schedule_startup_refresh(self, *, google_font_urls: tuple[str, ...] | None = None) -> None:
        if google_font_urls is not None:
            self._google_font_urls = google_font_urls
        if self._refresh_task is not None and not self._refresh_task.done():
            self._refresh_pending = True
            return
        self._refresh_task = asyncio.create_task(self.refresh_startup_assets(), name="font-asset-refresh")

    async def refresh_startup_assets(self) -> None:
        try:
            result = await asyncio.to_thread(self._refresh_font_assets)
        except Exception:
            log.exception("Font asset refresh failed")
        else:
            self._entries = result.entries
            log.info(
                "Font asset refresh complete: root=%s discovered=%s converted=%s",
                self._fonts_root,
                len(result.entries),
                result.converted_count,
            )
        if self._refresh_pending:
            self._refresh_pending = False
            self._refresh_task = asyncio.create_task(self.refresh_startup_assets(), name="font-asset-refresh")

    def _refresh_font_assets(self) -> FontAssetRefreshResult:
        if not self._fonts_root.exists() and not self._google_font_urls:
            return FontAssetRefreshResult(entries=(), converted_count=0)
        self._fonts_root.mkdir(parents=True, exist_ok=True)

        entries: list[FontAssetEntry] = []
        converted_count = 0
        entries.extend(self._refresh_google_font_assets())
        for source_path in self._font_source_paths():
            try:
                family_name = self._read_font_family_name(source_path)
                woff_path, woff_converted = self._ensure_converted_font(source_path, "woff")
                woff2_path, woff2_converted = self._ensure_converted_font(source_path, "woff2")
            except Exception:
                log.exception("Failed to process font asset: %s", source_path)
                continue
            converted_count += int(woff_converted) + int(woff2_converted)
            entries.append(
                FontAssetEntry(
                    family_name=family_name,
                    scope=self._font_scope(source_path),
                    source_path=source_path,
                    woff_path=woff_path,
                    woff2_path=woff2_path,
                )
            )
        return FontAssetRefreshResult(entries=tuple(entries), converted_count=converted_count)

    def _refresh_google_font_assets(self) -> tuple[FontAssetEntry, ...]:
        if not self._google_font_urls:
            return ()
        download_root = self._fonts_root / "_downloaded" / "google"
        download_root.mkdir(parents=True, exist_ok=True)
        entries: list[FontAssetEntry] = []
        for source_url in self._google_font_urls:
            try:
                entry = self._download_google_font_asset(download_root=download_root, source_url=source_url)
            except Exception:
                log.exception("Failed to download Google font source: %s", source_url)
                continue
            if entry is not None:
                entries.append(entry)
        return tuple(entries)

    def _download_google_font_asset(self, *, download_root: Path, source_url: str) -> FontAssetEntry | None:
        family_name = self._google_font_family_name(source_url)
        family_slug = self._slugify(family_name)
        family_root = download_root / family_slug
        family_root.mkdir(parents=True, exist_ok=True)
        css_path = family_root / "source.css"
        css_text = self._fetch_remote_text(source_url)
        css_path.write_text(css_text, encoding="utf-8")
        candidate_sources = self._google_font_block_sources(css_text)
        if not candidate_sources:
            raise ValueError("Google Fonts CSS did not expose any downloadable WOFF or WOFF2 sources.")
        downloaded_paths: dict[str, Path] = {}
        for index, (font_url, font_format) in enumerate(candidate_sources):
            target_suffix = ".woff2" if font_format.casefold() == "woff2" else ".woff"
            target_path = family_root / f"{index}{target_suffix}"
            self._download_remote_binary(font_url, target_path)
            downloaded_paths.setdefault(font_format.casefold(), target_path)
        woff2_path = downloaded_paths.get("woff2")
        woff_path = downloaded_paths.get("woff")
        if woff2_path is None and woff_path is None:
            return None
        return FontAssetEntry(
            family_name=family_name,
            scope=None,
            source_path=css_path,
            woff_path=woff_path,
            woff2_path=woff2_path,
        )

    def _google_font_family_name(self, source_url: str) -> str:
        family_query = urlsplit(source_url).query
        for key, value in parse_qsl(family_query, keep_blank_values=False):
            if key != "family":
                continue
            family_name = value.replace("+", " ").strip()
            if family_name:
                return family_name
        raise ValueError("Google font source URL is missing a family parameter.")

    def _google_font_block_sources(self, css_text: str) -> tuple[tuple[str, str], ...]:
        preferred_sources: list[tuple[str, str]] = []
        fallback_sources: list[tuple[str, str]] = []
        for match in _GOOGLE_FONT_BLOCK_RE.finditer(css_text):
            comment = (match.group("comment") or "").casefold()
            block_sources = [
                (source_match.group("url"), source_match.group("fmt").casefold())
                for source_match in _GOOGLE_FONT_SRC_RE.finditer(match.group("body"))
            ]
            if not block_sources:
                continue
            if "latin */" in comment or " latin " in comment:
                preferred_sources.extend(block_sources)
            else:
                fallback_sources.extend(block_sources)
        selected_sources = preferred_sources or fallback_sources
        deduplicated_sources: list[tuple[str, str]] = []
        seen_sources: set[tuple[str, str]] = set()
        for source in selected_sources:
            if source in seen_sources:
                continue
            seen_sources.add(source)
            deduplicated_sources.append(source)
        return tuple(deduplicated_sources[:2])

    def _font_source_paths(self) -> tuple[Path, ...]:
        return tuple(
            sorted(
                path
                for path in self._fonts_root.rglob("*")
                if path.is_file() and path.suffix.casefold() in _FONT_SOURCE_SUFFIXES
            )
        )

    def _font_scope(self, source_path: Path) -> str | None:
        relative_path = source_path.relative_to(self._fonts_root)
        if len(relative_path.parts) < 2:
            return None
        return relative_path.parts[0]

    def _read_font_family_name(self, source_path: Path) -> str:
        font = _load_ttfont(source_path)
        try:
            name_table = font["name"]
            if not hasattr(name_table, "names"):
                raise ValueError(f"Font name table is missing for {source_path}")
            typed_name_table = name_table
            for attribute_name in ("getBestFullName", "getBestFamilyName"):
                method = getattr(typed_name_table, attribute_name, None)
                if callable(method):
                    raw_name = method()
                    if isinstance(raw_name, str):
                        cleaned_name = raw_name.strip()
                        if cleaned_name:
                            return cleaned_name
            for record in getattr(typed_name_table, "names", ()):
                try:
                    raw_name = record.toUnicode()
                except Exception:
                    continue
                cleaned_name = raw_name.strip()
                if cleaned_name:
                    return cleaned_name
        finally:
            font.close()
        raise ValueError(f"Font family name is missing for {source_path}")

    def _ensure_converted_font(self, source_path: Path, flavor: FontFlavor) -> tuple[Path | None, bool]:
        target_path = source_path.with_suffix(f".{flavor}")
        if target_path.exists() and target_path.stat().st_mtime >= source_path.stat().st_mtime:
            return target_path, False

        try:
            self._write_converted_font(source_path=source_path, target_path=target_path, flavor=flavor)
        except ImportError as xcp:
            if flavor == "woff2" and "brotli" in str(xcp).casefold():
                if not self._woff2_missing_logged:
                    self._woff2_missing_logged = True
                    log.warning(
                        "WOFF2 font conversion is unavailable because the Brotli Python extension is not installed."
                    )
                return None, False
            raise
        return target_path, True

    @staticmethod
    def _write_converted_font(*, source_path: Path, target_path: Path, flavor: FontFlavor) -> None:
        font = _load_ttfont(source_path)
        try:
            font.flavor = flavor
            font.save(target_path)
        finally:
            font.close()

    def _public_entries(self) -> tuple[FontAssetEntry, ...]:
        return tuple(
            entry for entry in self._entries if entry.woff_path is not None or entry.woff2_path is not None
        )

    def _asset_url(self, *, base_path: str, asset_path: Path) -> str:
        relative_path = asset_path.relative_to(self._fonts_root)
        version_token = str(asset_path.stat().st_mtime_ns)
        return f"{base_path.rstrip('/')}/{quote(relative_path.as_posix(), safe='/')}?v={version_token}"

    @staticmethod
    def _css_string_literal(value: str) -> str:
        escaped_value = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped_value}"'

    @staticmethod
    def _fetch_remote_text(source_url: str) -> str:
        response = requests.get(source_url, timeout=30)
        response.raise_for_status()
        response.encoding = response.encoding or "utf-8"
        return response.text

    @staticmethod
    def _download_remote_binary(source_url: str, target_path: Path) -> None:
        response = requests.get(source_url, timeout=30)
        response.raise_for_status()
        target_path.write_bytes(response.content)

    @staticmethod
    def _slugify(value: str) -> str:
        base_slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or "font"
        hash_suffix = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
        return f"{base_slug}-{hash_suffix}"


font_assets = FontAssetRegistry()


def _load_ttfont(source_path: Path) -> _TTFontLike:
    try:
        from fontTools.ttLib import TTFont
    except ModuleNotFoundError as xcp:
        raise RuntimeError("Font conversion requires the `fonttools` package to be installed.") from xcp
    return _TTFontAdapter(TTFont(source_path))

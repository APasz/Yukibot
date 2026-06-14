from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from font_assets import FontAssetRegistry


class _FakeNameRecord:
    def __init__(self, value: str) -> None:
        self._value = value

    def toUnicode(self) -> str:
        return self._value


class _FakeNameTable:
    def __init__(self, value: str) -> None:
        self._value = value
        self.names = (_FakeNameRecord(value),)

    def getBestFullName(self) -> str:
        return self._value


class _FakeTTFont:
    saved_outputs: list[tuple[Path, str | None]] = []
    fail_woff2 = False

    def __init__(self, source_path: Path) -> None:
        self._source_path = source_path
        self.flavor: str | None = None

    def __getitem__(self, key: str) -> _FakeNameTable:
        if key != "name":
            raise KeyError(key)
        return _FakeNameTable(f"{self._source_path.stem} Family")

    def save(self, target_path: Path) -> None:
        if self.flavor == "woff2" and type(self).fail_woff2:
            raise ImportError("No module named brotli")
        target_path.write_text(self.flavor or "source", encoding="utf-8")
        type(self).saved_outputs.append((target_path, self.flavor))

    def close(self) -> None:
        return None


class FontAssetRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeTTFont.saved_outputs.clear()
        _FakeTTFont.fail_woff2 = False

    def test_refresh_font_assets_discovers_scoped_fonts_and_writes_woff_variants(self) -> None:
        with TemporaryDirectory() as temp_dir:
            fonts_root = Path(temp_dir) / "resources" / "fonts"
            scoped_font = fonts_root / "minecraft" / "blocky.ttf"
            global_font = fonts_root / "global.otf"
            scoped_font.parent.mkdir(parents=True, exist_ok=True)
            global_font.parent.mkdir(parents=True, exist_ok=True)
            scoped_font.write_text("ttf", encoding="utf-8")
            global_font.write_text("otf", encoding="utf-8")
            registry = FontAssetRegistry(fonts_root=fonts_root)

            with patch("font_assets._load_ttfont", side_effect=_FakeTTFont):
                result = registry._refresh_font_assets()
            registry._entries = result.entries

            self.assertEqual(result.converted_count, 4)
            self.assertEqual(len(result.entries), 2)
            self.assertEqual(result.entries[0].family_name, "global Family")
            self.assertIsNone(result.entries[0].scope)
            self.assertEqual(result.entries[1].family_name, "blocky Family")
            self.assertEqual(result.entries[1].scope, "minecraft")
            self.assertTrue(scoped_font.with_suffix(".woff").exists())
            self.assertTrue(scoped_font.with_suffix(".woff2").exists())
            self.assertTrue(global_font.with_suffix(".woff").exists())
            self.assertTrue(global_font.with_suffix(".woff2").exists())
            self.assertEqual(registry.available_font_families(scope="minecraft"), ("blocky Family", "global Family"))
            self.assertEqual(registry.available_font_families(scope="factorio"), ("global Family",))
            css_html = registry.font_face_css_html(base_path="/mod-web/assets/fonts")
            self.assertIn('font-family: "blocky Family";', css_html)
            self.assertIn('/mod-web/assets/fonts/minecraft/blocky.woff2?v=', css_html)
            self.assertIn('/mod-web/assets/fonts/global.woff?v=', css_html)

    def test_refresh_font_assets_skips_woff2_when_brotli_is_missing(self) -> None:
        with TemporaryDirectory() as temp_dir:
            fonts_root = Path(temp_dir) / "resources" / "fonts"
            source_path = fonts_root / "minecraft" / "blocky.ttf"
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_text("ttf", encoding="utf-8")
            registry = FontAssetRegistry(fonts_root=fonts_root)
            _FakeTTFont.fail_woff2 = True

            with patch("font_assets._load_ttfont", side_effect=_FakeTTFont):
                result = registry._refresh_font_assets()
            registry._entries = result.entries

            self.assertEqual(result.converted_count, 1)
            self.assertEqual(len(result.entries), 1)
            self.assertTrue(source_path.with_suffix(".woff").exists())
            self.assertFalse(source_path.with_suffix(".woff2").exists())
            self.assertIsNotNone(result.entries[0].woff_path)
            self.assertIsNone(result.entries[0].woff2_path)
            css_html = registry.font_face_css_html(base_path="/mod-web/assets/fonts")
            self.assertIn('/mod-web/assets/fonts/minecraft/blocky.woff', css_html)
            self.assertNotIn('format("woff2")', css_html)

    def test_refresh_font_assets_downloads_google_font_css_sources(self) -> None:
        with TemporaryDirectory() as temp_dir:
            fonts_root = Path(temp_dir) / "resources" / "fonts"
            registry = FontAssetRegistry(fonts_root=fonts_root)
            registry._google_font_urls = ("https://fonts.googleapis.com/css2?family=Black+Ops+One&display=swap",)
            downloaded_urls: list[str] = []

            def _fake_fetch_remote_text(source_url: str) -> str:
                self.assertEqual(source_url, "https://fonts.googleapis.com/css2?family=Black+Ops+One&display=swap")
                return """
                /* latin */
                @font-face {
                  font-family: 'Black Ops One';
                  src: url(https://fonts.gstatic.com/s/blackopsone/v1/latin.woff2) format('woff2');
                }
                """

            def _fake_download_remote_binary(source_url: str, target_path: Path) -> None:
                downloaded_urls.append(source_url)
                target_path.write_bytes(b"font")

            with (
                patch.object(FontAssetRegistry, "_fetch_remote_text", side_effect=_fake_fetch_remote_text),
                patch.object(FontAssetRegistry, "_download_remote_binary", side_effect=_fake_download_remote_binary),
            ):
                result = registry._refresh_font_assets()
            registry._entries = result.entries

            self.assertEqual(downloaded_urls, ["https://fonts.gstatic.com/s/blackopsone/v1/latin.woff2"])
            self.assertEqual(registry.available_font_families(scope="minecraft"), ("Black Ops One",))
            css_html = registry.font_face_css_html(base_path="/mod-web/assets/fonts")
            self.assertIn('font-family: "Black Ops One";', css_html)
            self.assertIn("/mod-web/assets/fonts/_downloaded/google/", css_html)
            self.assertIn("?v=", css_html)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import _minecraft_heads as minecraft_heads
from _minecraft_heads import MinecraftDefaultSkin, minecraft_default_skin_head_data_uri


class _FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content: bytes = content
        self.raise_for_status: Mock = Mock()


def _png_header(*, width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00"
    )


class MinecraftHeadTests(unittest.TestCase):
    def setUp(self) -> None:
        minecraft_heads._HEAD_DATA_URI_CACHE.clear()

    def test_downloads_missing_default_skin(self) -> None:
        skin_png: bytes = _png_header(width=64, height=64)
        response = _FakeResponse(skin_png)

        with tempfile.TemporaryDirectory() as directory:
            skin_directory = Path(directory)
            with (
                patch.object(minecraft_heads, "_DEFAULT_SKIN_DIRECTORY", skin_directory),
                patch.object(requests, "get", return_value=response) as get,
            ):
                data_uri = minecraft_default_skin_head_data_uri(MinecraftDefaultSkin.SUNNY)
                written_skin_png = (skin_directory / "sunny.png").read_bytes()

        self.assertIsNotNone(data_uri)
        self.assertEqual(written_skin_png, skin_png)
        get.assert_called_once_with(
            "https://assets.mcasset.cloud/latest/assets/minecraft/textures/entity/player/wide/sunny.png",
            timeout=10.0,
        )
        response.raise_for_status.assert_called_once_with()

    def test_download_failure_is_not_cached(self) -> None:
        skin_png: bytes = _png_header(width=64, height=64)

        with tempfile.TemporaryDirectory() as directory:
            skin_directory = Path(directory)
            with (
                patch.object(minecraft_heads, "_DEFAULT_SKIN_DIRECTORY", skin_directory),
                patch.object(
                    requests,
                    "get",
                    side_effect=(requests.ConnectionError("offline"), _FakeResponse(skin_png)),
                ) as get,
            ):
                first_data_uri = minecraft_default_skin_head_data_uri(MinecraftDefaultSkin.NOOR)
                second_data_uri = minecraft_default_skin_head_data_uri(MinecraftDefaultSkin.NOOR)

        self.assertIsNone(first_data_uri)
        self.assertIsNotNone(second_data_uri)
        self.assertEqual(get.call_count, 2)

    def test_rejects_downloaded_skin_with_unexpected_dimensions(self) -> None:
        response = _FakeResponse(_png_header(width=64, height=32))

        with tempfile.TemporaryDirectory() as directory:
            skin_directory = Path(directory)
            with (
                patch.object(minecraft_heads, "_DEFAULT_SKIN_DIRECTORY", skin_directory),
                patch.object(requests, "get", return_value=response),
            ):
                data_uri = minecraft_default_skin_head_data_uri(MinecraftDefaultSkin.STEVE)

            self.assertIsNone(data_uri)
            self.assertFalse((skin_directory / "steve.png").exists())


if __name__ == "__main__":
    unittest.main()

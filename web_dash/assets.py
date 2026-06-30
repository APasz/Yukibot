from __future__ import annotations

import gzip
import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from typing import cast

import brotli


class AssetContentEncoding(StrEnum):
    BROTLI = "br"
    GZIP = "gzip"


@dataclass(frozen=True, slots=True)
class EncodedAssetBody:
    content: bytes
    encoding: AssetContentEncoding | None


@dataclass(frozen=True, slots=True)
class CacheableTextAsset:
    content: bytes
    gzip_content: bytes
    brotli_content: bytes
    media_type: str
    version: str

    @classmethod
    def build(cls, *, text: str, media_type: str) -> "CacheableTextAsset":
        content = text.encode("utf-8")
        return cls(
            content=content,
            gzip_content=gzip.compress(content, compresslevel=9, mtime=0),
            brotli_content=cast(bytes, brotli.compress(content, quality=9)),
            media_type=media_type,
            version=hashlib.sha256(content).hexdigest()[:12],
        )

    def select_content(self, accept_encoding: str | None) -> EncodedAssetBody:
        qualities = _accepted_encoding_qualities(accept_encoding)
        brotli_quality = qualities.get(AssetContentEncoding.BROTLI.value, qualities.get("*", 0.0))
        gzip_quality = qualities.get(AssetContentEncoding.GZIP.value, qualities.get("*", 0.0))
        if brotli_quality > 0 and brotli_quality >= gzip_quality:
            return EncodedAssetBody(self.brotli_content, AssetContentEncoding.BROTLI)
        if gzip_quality > 0:
            return EncodedAssetBody(self.gzip_content, AssetContentEncoding.GZIP)
        return EncodedAssetBody(self.content, None)


@lru_cache(maxsize=16)
def cacheable_text_asset(text: str, media_type: str) -> CacheableTextAsset:
    return CacheableTextAsset.build(text=text, media_type=media_type)


def extract_html_tag_contents(html: str, *, tag_name: str) -> str:
    if not tag_name.isalpha():
        raise ValueError("HTML asset tag names must contain only letters.")
    blocks = re.findall(
        rf"<{tag_name}(?:\s[^>]*)?>\s*(.*?)\s*</{tag_name}>",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if not blocks:
        raise ValueError(f"HTML asset does not contain a {tag_name} block.")
    return "\n".join(blocks)


def _accepted_encoding_qualities(header: str | None) -> dict[str, float]:
    if header is None:
        return {}
    qualities: dict[str, float] = {}
    for raw_entry in header.split(","):
        parts = tuple(part.strip() for part in raw_entry.split(";") if part.strip())
        if not parts:
            continue
        encoding = parts[0].casefold()
        quality = 1.0
        for parameter in parts[1:]:
            name, separator, raw_value = parameter.partition("=")
            if separator and name.strip().casefold() == "q":
                try:
                    quality = float(raw_value)
                except ValueError:
                    quality = 0.0
                quality = min(1.0, max(0.0, quality))
        qualities[encoding] = quality
    return qualities

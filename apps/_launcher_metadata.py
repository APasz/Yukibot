from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import cast
from urllib.parse import unquote, urlsplit

import httpx
from modmux import Muxer
from modmux.models import ModID, Provider
from modmux.modmux_errors import ModMuxError
from modmux.providers.curseforge import CurseforgeCreds
from modmux.utils.urls import parse_url
from pydantic import SecretStr

import config
from apps._config import (
    CurseForgeFileReference,
    CurseForgeModMetadata,
    LauncherProviderUrls,
    ModPlatformMetadata,
    ModrinthModMetadata,
    launcher_provider_label,
    mod_capabilities_for_scope,
)


log = logging.getLogger(__name__)


def _provider_mod_id(page_url: str, expected_provider: Provider) -> ModID:
    mod_id = parse_url(page_url)
    if mod_id is None or mod_id.provider is not expected_provider:
        raise ValueError(f"URL is not a valid {launcher_provider_label(expected_provider)} mod page: {page_url}")
    return mod_id


def _version_reference(page_url: str, provider: Provider) -> str:
    segments = [unquote(segment) for segment in urlsplit(page_url).path.split("/") if segment]
    marker = "version" if provider is Provider.MODRINTH else "files"
    try:
        marker_index = segments.index(marker)
        reference = segments[marker_index + 1]
    except (ValueError, IndexError) as xcp:
        raise ValueError(f"{launcher_provider_label(provider)} URL must identify a specific file or version.") from xcp
    if not reference.strip():
        raise ValueError(f"{launcher_provider_label(provider)} URL has an empty file or version identifier.")
    return reference


def _required_mapping(raw: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{label} returned invalid metadata.")
    return cast(Mapping[str, object], raw)


def _required_text(payload: Mapping[str, object], key: str, *, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} metadata is missing {key}.")
    return value.strip()


def _required_positive_int(payload: Mapping[str, object], key: str, *, label: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} metadata is missing a positive integer {key}.")
    return value


async def _resolve_modrinth(
    page_url: str,
    *,
    local_filename: str,
    http: httpx.AsyncClient,
) -> ModrinthModMetadata:
    mod_id = _provider_mod_id(page_url, Provider.MODRINTH)
    version_reference = _version_reference(page_url, Provider.MODRINTH)
    response = await http.get(
        f"https://api.modrinth.com/v2/project/{mod_id.id}/version",
        timeout=30,
    )
    response.raise_for_status()
    raw_versions = cast(object, response.json())
    if not isinstance(raw_versions, list):
        raise ValueError("Modrinth returned invalid version metadata.")

    version: Mapping[str, object] | None = None
    for raw_version in cast(list[object], raw_versions):
        candidate = _required_mapping(raw_version, label="Modrinth version")
        if version_reference in {candidate.get("id"), candidate.get("version_number")}:
            version = candidate
            break
    if version is None:
        raise ValueError(f"Modrinth version {version_reference!r} was not found for {mod_id.id!r}.")

    raw_files = version.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ValueError("Modrinth version has no downloadable files.")
    files = tuple(
        _required_mapping(raw_file, label="Modrinth file")
        for raw_file in cast(list[object], raw_files)
    )
    selected_file = next(
        (
            file_payload
            for file_payload in files
            if str(file_payload.get("filename", "")).casefold() == local_filename.casefold()
        ),
        None,
    )
    if selected_file is None:
        selected_file = next((file_payload for file_payload in files if file_payload.get("primary") is True), files[0])

    hashes = _required_mapping(selected_file.get("hashes"), label="Modrinth file hashes")

    return ModrinthModMetadata(
        page_url=page_url,
        project_id=_required_text(version, "project_id", label="Modrinth version"),
        version_id=_required_text(version, "id", label="Modrinth version"),
        download_url=_required_text(selected_file, "url", label="Modrinth file"),
        filename=_required_text(selected_file, "filename", label="Modrinth file"),
        sha1=_required_text(hashes, "sha1", label="Modrinth file hashes"),
        sha512=_required_text(hashes, "sha512", label="Modrinth file hashes"),
        size=_required_positive_int(selected_file, "size", label="Modrinth file"),
    )


def _curseforge_api_key() -> str | None:
    value = (config.env_opt("CURSEFORGE_API_KEY") or "").strip()
    return value or None


def has_curseforge_api_key() -> bool:
    return _curseforge_api_key() is not None


def _modmux_credentials() -> list[CurseforgeCreds]:
    api_key = _curseforge_api_key()
    return [] if api_key is None else [CurseforgeCreds(api_key=SecretStr(api_key))]


async def _curseforge_metadata_from_reference(
    reference: CurseForgeFileReference,
    *,
    http: httpx.AsyncClient,
) -> CurseForgeModMetadata:
    api_key = _curseforge_api_key()
    if api_key is None:
        log.warning(
            "CurseForge project/file pair is unverified because CURSEFORGE_API_KEY is unavailable: "
            "project_id=%s file_id=%s",
            reference.project_id,
            reference.file_id,
        )
        return CurseForgeModMetadata(project_id=reference.project_id, file_id=reference.file_id)

    response = await http.get(
        f"https://api.curseforge.com/v1/mods/{reference.project_id}/files/{reference.file_id}",
        headers={"x-api-key": api_key},
        timeout=30,
    )
    response.raise_for_status()
    payload = _required_mapping(cast(object, response.json()), label="CurseForge file response")
    file_payload = _required_mapping(payload.get("data"), label="CurseForge file")
    project_id = _required_positive_int(file_payload, "modId", label="CurseForge file")
    file_id = _required_positive_int(file_payload, "id", label="CurseForge file")
    if (project_id, file_id) != (reference.project_id, reference.file_id):
        raise ValueError(
            "CurseForge returned a different project/file pair: "
            f"expected {reference.project_id}/{reference.file_id}, got {project_id}/{file_id}."
        )
    return CurseForgeModMetadata(project_id=project_id, file_id=file_id)


async def _resolve_curseforge(
    page_url: str,
    *,
    http: httpx.AsyncClient,
) -> CurseForgeModMetadata:
    mod_id = _provider_mod_id(page_url, Provider.CURSEFORGE)
    file_reference = _version_reference(page_url, Provider.CURSEFORGE)
    if not file_reference.isdecimal():
        raise ValueError("CurseForge file URL must end with a numeric file ID.")
    credentials = _modmux_credentials()
    if not credentials:
        raise ValueError("CURSEFORGE_API_KEY is required to resolve CurseForge file pages.")

    try:
        async with Muxer(creds=credentials, http=http) as muxer:
            resolved_mod = await muxer.get_mod(Provider.CURSEFORGE, mod_id, author_resolution=False)
    except ModMuxError as xcp:
        raise ValueError(f"CurseForge metadata lookup failed: {xcp}") from xcp
    if not resolved_mod.id.id.isdecimal():
        raise ValueError("CurseForge returned a non-numeric project ID.")
    return CurseForgeModMetadata(
        page_url=page_url,
        project_id=int(resolved_mod.id.id),
        file_id=int(file_reference),
    )


async def _resolve_curseforge_source(
    urls: LauncherProviderUrls,
    *,
    http: httpx.AsyncClient,
) -> CurseForgeModMetadata | None:
    if urls.curseforge_reference is not None:
        return await _curseforge_metadata_from_reference(urls.curseforge_reference, http=http)
    if urls.curseforge is None:
        return None
    return await _resolve_curseforge(urls.curseforge, http=http)


async def resolve_launcher_metadata(
    *,
    scope: str,
    urls: LauncherProviderUrls,
    local_filename: str,
    http: httpx.AsyncClient | None = None,
) -> ModPlatformMetadata:
    capabilities = mod_capabilities_for_scope(scope)
    supported = frozenset(capabilities.launcher_metadata_providers)
    supplied_urls = {
        provider: page_url
        for provider in (Provider.MODRINTH, Provider.CURSEFORGE)
        if (page_url := urls.for_provider(provider)) is not None
    }
    supplied_providers = {
        provider for provider in (Provider.MODRINTH, Provider.CURSEFORGE) if urls.has_provider(provider)
    }
    unsupported = tuple(provider for provider in supplied_providers if provider not in supported)
    if unsupported:
        names = ", ".join(launcher_provider_label(provider) for provider in unsupported)
        raise ValueError(f"{scope} does not support launcher metadata from: {names}.")

    owned_http = http is None
    client = http or httpx.AsyncClient()
    try:
        try:
            modrinth = (
                await _resolve_modrinth(
                    supplied_urls[Provider.MODRINTH], local_filename=local_filename, http=client
                )
                if Provider.MODRINTH in supplied_urls
                else None
            )
            curseforge = await _resolve_curseforge_source(urls, http=client)
        except httpx.HTTPError as xcp:
            raise ValueError(f"Launcher metadata lookup failed: {xcp}") from xcp
        return ModPlatformMetadata(modrinth=modrinth, curseforge=curseforge)
    finally:
        if owned_http:
            await client.aclose()

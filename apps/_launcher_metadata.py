from __future__ import annotations

import asyncio
import enum
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import httpx
from modmux import Muxer
from modmux.models import ModID, Provider
from modmux.modmux_errors import ModMuxError
from modmux.providers.curseforge import CurseforgeCreds
from modmux.utils.urls import parse_url
from pydantic import SecretStr

import config
from apps._config import (
    BulkLauncherMetadataDiscovery,
    BulkLauncherMetadataEntry,
    BulkLauncherMetadataStatus,
    CurseForgeFileReference,
    CurseForgeModMetadata,
    KnownModPageProvider,
    LauncherMetadataCandidate,
    LauncherMetadataDiscovery,
    LauncherMetadataMatchReason,
    LauncherMetadataProviderCandidates,
    LauncherMetadataProviderError,
    LauncherMetadataReleaseChannel,
    LauncherMetadataResolution,
    LauncherProviderUrls,
    ModPageCandidate,
    ModPageDiscovery,
    ModPageLink,
    ModPageMatchConfidence,
    ModPageMatchReason,
    ModPageProviderCandidates,
    ModPlatformMetadata,
    ModrinthModMetadata,
    ModType,
    known_mod_page_provider_for_url,
    launcher_provider_label,
    mod_capabilities_for_scope,
)

log = logging.getLogger(__name__)


def _metadata_provider_error(
    provider: Provider,
    exception: ValueError | httpx.HTTPError,
) -> LauncherMetadataProviderError:
    message = str(exception).strip() or type(exception).__name__
    return LauncherMetadataProviderError(provider=provider, message=message)


def _selected_launcher_metadata_providers(
    *,
    scope: str,
    supported: tuple[Provider, ...],
    requested: tuple[Provider, ...] | None,
) -> tuple[Provider, ...]:
    if requested is None:
        return supported
    if not requested:
        raise ValueError("Select at least one launcher metadata provider.")
    if len(requested) != len(set(requested)):
        raise ValueError("Launcher metadata providers must be unique.")
    unsupported = tuple(provider for provider in requested if provider not in supported)
    if unsupported:
        names = ", ".join(launcher_provider_label(provider) for provider in unsupported)
        raise ValueError(f"{scope} does not support launcher metadata from: {names}.")
    return requested


class ModrinthSideSupport(enum.StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"

    @property
    def supported(self) -> bool:
        return self in {ModrinthSideSupport.REQUIRED, ModrinthSideSupport.OPTIONAL}


@dataclass(frozen=True, slots=True)
class _LocalFileIdentity:
    filename: str
    size: int
    sha1: str
    curseforge_fingerprint: int


@dataclass(frozen=True, slots=True)
class BulkLauncherMetadataTarget:
    mod_name: str
    friendly_name: str
    local_path: Path
    existing_mod_pages: tuple[ModPageLink, ...]
    existing_platforms: ModPlatformMetadata


@dataclass(frozen=True, slots=True)
class _BulkModrinthMatch:
    mod_page: ModPageLink
    metadata: ModrinthModMetadata
    suggested_mod_type: ModType | None


@dataclass(frozen=True, slots=True)
class _BulkCurseForgeMatch:
    mod_page: ModPageLink
    metadata: CurseForgeModMetadata


_CURSEFORGE_FINGERPRINT_WHITESPACE = frozenset({9, 10, 13, 32})
_UINT32_MASK = 0xFFFFFFFF


def _murmur2_32(data: bytes | bytearray, *, seed: int = 1) -> int:
    multiplier = 0x5BD1E995
    value = (seed ^ len(data)) & _UINT32_MASK
    block_end = len(data) - (len(data) % 4)
    for offset in range(0, block_end, 4):
        block = int.from_bytes(data[offset : offset + 4], byteorder="little")
        block = (block * multiplier) & _UINT32_MASK
        block ^= block >> 24
        block = (block * multiplier) & _UINT32_MASK
        value = (value * multiplier) & _UINT32_MASK
        value ^= block

    tail = data[block_end:]
    if len(tail) == 3:
        value ^= tail[2] << 16
    if len(tail) >= 2:
        value ^= tail[1] << 8
    if tail:
        value ^= tail[0]
        value = (value * multiplier) & _UINT32_MASK

    value ^= value >> 13
    value = (value * multiplier) & _UINT32_MASK
    value ^= value >> 15
    return value & _UINT32_MASK


def _curseforge_fingerprint(data: bytes) -> int:
    normalised = bytes(value for value in data if value not in _CURSEFORGE_FINGERPRINT_WHITESPACE)
    return _murmur2_32(normalised)


def _local_file_identity(
    path: Path,
    *,
    logical_filename: str | None = None,
) -> _LocalFileIdentity:
    filename = path.name if logical_filename is None else logical_filename.strip()
    if not filename or Path(filename).name != filename:
        raise ValueError("Local mod filename must be a single file name.")
    digest = hashlib.sha1(usedforsecurity=False)
    fingerprint_data = bytearray()
    size = 0
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
            fingerprint_data.extend(
                value for value in chunk if value not in _CURSEFORGE_FINGERPRINT_WHITESPACE
            )
    return _LocalFileIdentity(
        filename=filename,
        size=size,
        sha1=digest.hexdigest(),
        curseforge_fingerprint=_murmur2_32(fingerprint_data),
    )


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _text_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(text for item in cast(list[object], value) if (text := _optional_text(item)) is not None)


def _release_channel(value: object) -> LauncherMetadataReleaseChannel:
    try:
        return LauncherMetadataReleaseChannel(value)
    except (TypeError, ValueError):
        return LauncherMetadataReleaseChannel.UNKNOWN


def _match_reasons(
    *,
    local: _LocalFileIdentity,
    remote_filename: str,
    remote_size: int | None,
    remote_sha1: str | None,
) -> tuple[LauncherMetadataMatchReason, ...]:
    reasons: list[LauncherMetadataMatchReason] = []
    if remote_sha1 is not None and remote_sha1.casefold() == local.sha1:
        reasons.append(LauncherMetadataMatchReason.SHA1)
    if remote_filename.casefold() == local.filename.casefold():
        reasons.append(
            LauncherMetadataMatchReason.FILENAME_AND_SIZE
            if remote_size == local.size
            else LauncherMetadataMatchReason.FILENAME
        )
    return tuple(reasons)


def _candidate_sort_key(
    candidate: LauncherMetadataCandidate,
    *,
    game_version: str | None,
    loader: str | None,
) -> tuple[int, int, int]:
    reason_rank = min(
        {
            LauncherMetadataMatchReason.EXPLICIT_FILE_PAGE: 0,
            LauncherMetadataMatchReason.SHA1: 1,
            LauncherMetadataMatchReason.FILENAME_AND_SIZE: 2,
            LauncherMetadataMatchReason.FILENAME: 3,
        }[reason]
        for reason in candidate.match_reasons
    )
    game_rank = 0 if game_version is not None and game_version in candidate.game_versions else 1
    loader_rank = 0 if loader is not None and loader.casefold() in {
        candidate_loader.casefold() for candidate_loader in candidate.loaders
    } else 1
    return reason_rank, game_rank, loader_rank


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


def launcher_project_page_url(page_url: str, provider: Provider) -> str:
    if provider not in {Provider.MODRINTH, Provider.CURSEFORGE}:
        raise ValueError(f"Launcher project pages do not support {provider.value}.")
    _provider_mod_id(page_url, provider)
    _version_reference(page_url, provider)
    parsed = urlsplit(page_url)
    segments = [unquote(segment) for segment in parsed.path.split("/") if segment]
    marker = "version" if provider is Provider.MODRINTH else "files"
    marker_index = segments.index(marker)
    if marker_index < 2:
        raise ValueError(f"{launcher_provider_label(provider)} URL has an invalid project path.")
    assert parsed.hostname is not None
    project_path = "/" + "/".join(segments[:marker_index])
    return urlunsplit(("https", parsed.hostname.casefold(), project_path, "", ""))


def _normalise_launcher_project_page(page_url: str, provider: Provider) -> str:
    _provider_mod_id(page_url, provider)
    parsed = urlsplit(page_url)
    segments = [unquote(segment) for segment in parsed.path.split("/") if segment]
    marker = "version" if provider is Provider.MODRINTH else "files"
    if marker in segments:
        return launcher_project_page_url(page_url, provider)
    assert parsed.hostname is not None
    return urlunsplit(
        (
            "https",
            parsed.hostname.casefold(),
            "/" + "/".join(segments),
            "",
            "",
        )
    )


@dataclass(frozen=True, slots=True)
class _LauncherProjectPageCollection:
    project_pages: dict[Provider, str]
    explicit_file_pages: dict[Provider, str]
    errors: dict[Provider, LauncherMetadataProviderCandidates]


def _normalise_explicit_launcher_file_page(
    page_url: str,
    provider: Provider,
) -> str | None:
    parsed = urlsplit(page_url)
    segments = [unquote(segment) for segment in parsed.path.split("/") if segment]
    marker = "version" if provider is Provider.MODRINTH else "files"
    if marker not in segments:
        return None
    _version_reference(page_url, provider)
    assert parsed.hostname is not None
    return urlunsplit(
        ("https", parsed.hostname.casefold(), "/" + "/".join(segments), "", "")
    )


def _collect_launcher_project_pages(
    mod_pages: tuple[ModPageLink, ...],
    *,
    supported: frozenset[Provider],
    excluded: frozenset[Provider],
) -> _LauncherProjectPageCollection:
    known_to_launcher = {
        KnownModPageProvider.MODRINTH: Provider.MODRINTH,
        KnownModPageProvider.CURSEFORGE: Provider.CURSEFORGE,
    }
    project_pages: dict[Provider, str] = {}
    explicit_file_pages: dict[Provider, str] = {}
    errors: dict[Provider, LauncherMetadataProviderCandidates] = {}
    for mod_page in mod_pages:
        known_provider = known_mod_page_provider_for_url(mod_page.url)
        if known_provider is None:
            continue
        provider = known_to_launcher.get(known_provider)
        if provider is None or provider not in supported or provider in excluded:
            continue
        if provider in errors:
            continue
        try:
            project_page = _normalise_launcher_project_page(mod_page.url, provider)
            explicit_file_page = _normalise_explicit_launcher_file_page(
                mod_page.url,
                provider,
            )
        except ValueError as xcp:
            project_pages.pop(provider, None)
            explicit_file_pages.pop(provider, None)
            errors[provider] = LauncherMetadataProviderCandidates(
                provider=provider,
                project_page_url=mod_page.url,
                error=str(xcp),
            )
            continue
        existing = project_pages.get(provider)
        if existing is not None and existing != project_page:
            project_pages.pop(provider, None)
            explicit_file_pages.pop(provider, None)
            errors[provider] = LauncherMetadataProviderCandidates(
                provider=provider,
                project_page_url=mod_page.url,
                error=(
                    f"Multiple {launcher_provider_label(provider)} project pages were supplied."
                ),
            )
            continue
        existing_file_page = explicit_file_pages.get(provider)
        if (
            explicit_file_page is not None
            and existing_file_page is not None
            and existing_file_page != explicit_file_page
        ):
            project_pages.pop(provider, None)
            explicit_file_pages.pop(provider, None)
            errors[provider] = LauncherMetadataProviderCandidates(
                provider=provider,
                project_page_url=mod_page.url,
                error=f"Multiple {launcher_provider_label(provider)} file pages were supplied.",
            )
            continue
        project_pages[provider] = project_page
        if explicit_file_page is not None:
            explicit_file_pages[provider] = explicit_file_page
    return _LauncherProjectPageCollection(
        project_pages=project_pages,
        explicit_file_pages=explicit_file_pages,
        errors=errors,
    )


def _candidates_for_explicit_file_page(
    candidates: tuple[LauncherMetadataCandidate, ...],
    *,
    provider: Provider,
    file_page_url: str | None,
) -> tuple[LauncherMetadataCandidate, ...]:
    if file_page_url is None:
        return candidates
    explicit_reference = _version_reference(file_page_url, provider)
    matching = tuple(
        candidate.model_copy(
            update={
                "match_reasons": (
                    LauncherMetadataMatchReason.EXPLICIT_FILE_PAGE,
                    *candidate.match_reasons,
                )
            }
        )
        for candidate in candidates
        if explicit_reference
        in {
            _version_reference(candidate.file_page_url, provider),
            candidate.version,
        }
    )
    return matching or candidates


def _modrinth_candidate(
    *,
    project_page_url: str,
    version: Mapping[str, object],
    file_payload: Mapping[str, object],
    local: _LocalFileIdentity,
) -> LauncherMetadataCandidate | None:
    filename = _required_text(file_payload, "filename", label="Modrinth file")
    size = _required_positive_int(file_payload, "size", label="Modrinth file")
    hashes = _required_mapping(file_payload.get("hashes"), label="Modrinth file hashes")
    sha1 = _optional_text(hashes.get("sha1"))
    reasons = _match_reasons(
        local=local,
        remote_filename=filename,
        remote_size=size,
        remote_sha1=sha1,
    )
    if not reasons:
        return None
    version_id = _required_text(version, "id", label="Modrinth version")
    return LauncherMetadataCandidate(
        provider=Provider.MODRINTH,
        project_page_url=project_page_url,
        file_page_url=f"{project_page_url}/version/{quote(version_id, safe='')}",
        version=_required_text(version, "version_number", label="Modrinth version"),
        filename=filename,
        size=size,
        game_versions=_text_tuple(version.get("game_versions")),
        loaders=_text_tuple(version.get("loaders")),
        release_channel=_release_channel(version.get("version_type")),
        match_reasons=reasons,
    )


async def _discover_modrinth_candidates(
    project_page_url: str,
    *,
    local: _LocalFileIdentity,
    game_version: str | None,
    loader: str | None,
    http: httpx.AsyncClient,
) -> tuple[LauncherMetadataCandidate, ...]:
    mod_id = _provider_mod_id(project_page_url, Provider.MODRINTH)
    response = await http.get(
        f"https://api.modrinth.com/v2/project/{mod_id.id}/version",
        params={"include_changelog": "false"},
        timeout=30,
    )
    response.raise_for_status()
    raw_versions = cast(object, response.json())
    if not isinstance(raw_versions, list):
        raise ValueError("Modrinth returned invalid version metadata.")

    candidates_by_page: dict[str, LauncherMetadataCandidate] = {}
    for raw_version in cast(list[object], raw_versions):
        version = _required_mapping(raw_version, label="Modrinth version")
        raw_files = version.get("files")
        if not isinstance(raw_files, list):
            raise ValueError("Modrinth version returned invalid file metadata.")
        for raw_file in cast(list[object], raw_files):
            candidate = _modrinth_candidate(
                project_page_url=project_page_url,
                version=version,
                file_payload=_required_mapping(raw_file, label="Modrinth file"),
                local=local,
            )
            if candidate is None:
                continue
            previous = candidates_by_page.get(candidate.file_page_url)
            if previous is None or _candidate_sort_key(
                candidate,
                game_version=game_version,
                loader=loader,
            ) < _candidate_sort_key(previous, game_version=game_version, loader=loader):
                candidates_by_page[candidate.file_page_url] = candidate
    return tuple(
        sorted(
            candidates_by_page.values(),
            key=lambda candidate: _candidate_sort_key(
                candidate,
                game_version=game_version,
                loader=loader,
            ),
        )
    )


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


def _required_modrinth_side_support(
    payload: Mapping[str, object],
    key: str,
) -> ModrinthSideSupport:
    value = payload.get(key)
    try:
        return ModrinthSideSupport(value)
    except (TypeError, ValueError) as xcp:
        raise ValueError(f"Modrinth project metadata has invalid {key}.") from xcp


def _suggest_mod_type_from_modrinth(
    *,
    client: ModrinthSideSupport,
    server: ModrinthSideSupport,
) -> ModType | None:
    if ModrinthSideSupport.UNKNOWN in {client, server}:
        return None
    if client is ModrinthSideSupport.UNSUPPORTED and server.supported:
        return ModType.SERVER
    if server is ModrinthSideSupport.UNSUPPORTED and client.supported:
        return ModType.CLIENT
    if client.supported and server.supported:
        return ModType.REGULAR
    return None


async def _resolve_modrinth(
    page_url: str,
    *,
    local_filename: str,
    local_sha1: str | None,
    http: httpx.AsyncClient,
) -> tuple[ModrinthModMetadata, ModType | None]:
    mod_id = _provider_mod_id(page_url, Provider.MODRINTH)
    version_reference = _version_reference(page_url, Provider.MODRINTH)
    versions_response, project_response = await asyncio.gather(
        http.get(
            f"https://api.modrinth.com/v2/project/{mod_id.id}/version",
            timeout=30,
        ),
        http.get(
            f"https://api.modrinth.com/v2/project/{mod_id.id}",
            timeout=30,
        ),
    )
    versions_response.raise_for_status()
    project_response.raise_for_status()
    raw_versions = cast(object, versions_response.json())
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
            if local_sha1 is not None
            and _optional_text(
                _required_mapping(file_payload.get("hashes"), label="Modrinth file hashes").get("sha1")
            )
            == local_sha1
        ),
        None,
    )
    if selected_file is None:
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

    project = _required_mapping(cast(object, project_response.json()), label="Modrinth project")
    suggested_mod_type = _suggest_mod_type_from_modrinth(
        client=_required_modrinth_side_support(project, "client_side"),
        server=_required_modrinth_side_support(project, "server_side"),
    )

    return ModrinthModMetadata(
        page_url=page_url,
        project_id=_required_text(version, "project_id", label="Modrinth version"),
        version_id=_required_text(version, "id", label="Modrinth version"),
        download_url=_required_text(selected_file, "url", label="Modrinth file"),
        description=_optional_text(project.get("description")),
        filename=_required_text(selected_file, "filename", label="Modrinth file"),
        sha1=_required_text(hashes, "sha1", label="Modrinth file hashes"),
        sha512=_required_text(hashes, "sha512", label="Modrinth file hashes"),
        size=_required_positive_int(selected_file, "size", label="Modrinth file"),
    ), suggested_mod_type


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
    return CurseForgeModMetadata(
        project_id=project_id,
        file_id=file_id,
        description=await _curseforge_project_description(project_id, http=http),
    )


async def _resolve_curseforge(
    page_url: str,
    *,
    http: httpx.AsyncClient,
) -> CurseForgeModMetadata:
    mod_id = _provider_mod_id(page_url, Provider.CURSEFORGE)
    file_reference = _version_reference(page_url, Provider.CURSEFORGE)
    if not file_reference.isdecimal():
        raise ValueError("CurseForge file URL must end with a numeric file ID.")
    project_id = await _resolve_curseforge_project_id(mod_id, http=http)
    return CurseForgeModMetadata(
        page_url=page_url,
        project_id=project_id,
        file_id=int(file_reference),
        description=await _curseforge_project_description(project_id, http=http),
    )


async def _resolve_curseforge_project_id(
    mod_id: ModID,
    *,
    http: httpx.AsyncClient,
) -> int:
    credentials = _modmux_credentials()
    if not credentials:
        raise ValueError("CURSEFORGE_API_KEY is required to resolve CurseForge pages.")

    try:
        async with Muxer(creds=credentials, http=http) as muxer:
            resolved_mod = await muxer.get_mod(Provider.CURSEFORGE, mod_id, author_resolution=False)
    except ModMuxError as xcp:
        raise ValueError(f"CurseForge metadata lookup failed: {xcp}") from xcp
    if not resolved_mod.id.id.isdecimal():
        raise ValueError("CurseForge returned a non-numeric project ID.")
    return int(resolved_mod.id.id)


def _curseforge_sha1(file_payload: Mapping[str, object]) -> str | None:
    raw_hashes = file_payload.get("hashes")
    if not isinstance(raw_hashes, list):
        return None
    for raw_hash in cast(list[object], raw_hashes):
        file_hash = _required_mapping(raw_hash, label="CurseForge file hash")
        if file_hash.get("algo") == 1:
            return _optional_text(file_hash.get("value"))
    return None


async def _curseforge_project_description(
    project_id: int,
    *,
    http: httpx.AsyncClient,
) -> str | None:
    api_key = _curseforge_api_key()
    if api_key is None:
        return None
    response = await http.get(
        f"https://api.curseforge.com/v1/mods/{project_id}",
        headers={"x-api-key": api_key},
        timeout=30,
    )
    response.raise_for_status()
    payload = _required_mapping(cast(object, response.json()), label="CurseForge project response")
    project = _required_mapping(payload.get("data"), label="CurseForge project")
    return _optional_text(project.get("summary"))


def _curseforge_release_channel(value: object) -> LauncherMetadataReleaseChannel:
    if isinstance(value, bool) or not isinstance(value, int):
        return LauncherMetadataReleaseChannel.UNKNOWN
    channels: dict[int, LauncherMetadataReleaseChannel] = {
        1: LauncherMetadataReleaseChannel.RELEASE,
        2: LauncherMetadataReleaseChannel.BETA,
        3: LauncherMetadataReleaseChannel.ALPHA,
    }
    return channels.get(value, LauncherMetadataReleaseChannel.UNKNOWN)


def _curseforge_candidate(
    *,
    project_page_url: str,
    file_payload: Mapping[str, object],
    local: _LocalFileIdentity,
) -> LauncherMetadataCandidate | None:
    filename = _required_text(file_payload, "fileName", label="CurseForge file")
    size = _required_positive_int(file_payload, "fileLength", label="CurseForge file")
    reasons = _match_reasons(
        local=local,
        remote_filename=filename,
        remote_size=size,
        remote_sha1=_curseforge_sha1(file_payload),
    )
    if not reasons:
        return None
    file_id = _required_positive_int(file_payload, "id", label="CurseForge file")
    compatibility = _text_tuple(file_payload.get("gameVersions"))
    known_loaders = frozenset({"forge", "fabric", "quilt", "neoforge", "liteloader", "cauldron"})
    loaders = tuple(value for value in compatibility if value.casefold() in known_loaders)
    game_versions = tuple(value for value in compatibility if value.casefold() not in known_loaders)
    return LauncherMetadataCandidate(
        provider=Provider.CURSEFORGE,
        project_page_url=project_page_url,
        file_page_url=f"{project_page_url}/files/{file_id}",
        version=_optional_text(file_payload.get("displayName")) or filename,
        filename=filename,
        size=size,
        game_versions=game_versions,
        loaders=loaders,
        release_channel=_curseforge_release_channel(file_payload.get("releaseType")),
        match_reasons=reasons,
    )


async def _discover_curseforge_candidates(
    project_page_url: str,
    *,
    local: _LocalFileIdentity,
    game_version: str | None,
    loader: str | None,
    http: httpx.AsyncClient,
) -> tuple[LauncherMetadataCandidate, ...]:
    mod_id = _provider_mod_id(project_page_url, Provider.CURSEFORGE)
    project_id = await _resolve_curseforge_project_id(mod_id, http=http)
    headers = {"x-api-key": _curseforge_api_key() or ""}
    candidates: list[LauncherMetadataCandidate] = []
    index = 0
    page_size = 50
    total_count: int | None = None
    while total_count is None or index < total_count:
        response = await http.get(
            f"https://api.curseforge.com/v1/mods/{project_id}/files",
            headers=headers,
            params={"index": index, "pageSize": page_size},
            timeout=30,
        )
        response.raise_for_status()
        payload = _required_mapping(cast(object, response.json()), label="CurseForge files response")
        raw_files = payload.get("data")
        pagination = _required_mapping(payload.get("pagination"), label="CurseForge files pagination")
        if not isinstance(raw_files, list):
            raise ValueError("CurseForge returned invalid file metadata.")
        result_count = pagination.get("resultCount")
        total_count_value = pagination.get("totalCount")
        if (
            isinstance(result_count, bool)
            or not isinstance(result_count, int)
            or result_count < 0
            or isinstance(total_count_value, bool)
            or not isinstance(total_count_value, int)
            or total_count_value < 0
        ):
            raise ValueError("CurseForge returned invalid file pagination.")
        total_count = total_count_value
        for raw_file in cast(list[object], raw_files):
            candidate = _curseforge_candidate(
                project_page_url=project_page_url,
                file_payload=_required_mapping(raw_file, label="CurseForge file"),
                local=local,
            )
            if candidate is not None:
                candidates.append(candidate)
        if result_count == 0:
            break
        index += result_count
    return tuple(
        sorted(
            candidates,
            key=lambda candidate: _candidate_sort_key(
                candidate,
                game_version=game_version,
                loader=loader,
            ),
        )
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


def _normalised_project_name(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _project_search_terms(
    *,
    friendly_name: str,
    local_filename: str,
    detected_version: str | None,
    game_version: str | None,
    loader: str | None,
) -> tuple[str, ...]:
    filename_stem = Path(local_filename).stem
    cleaned_stem = filename_stem
    for token in (detected_version, game_version, loader):
        if token is None or not token.strip():
            continue
        cleaned_stem = re.sub(re.escape(token), " ", cleaned_stem, flags=re.IGNORECASE)
    cleaned_stem = re.sub(
        r"(?<![A-Za-z0-9])v?\d+(?:[._-]\d+)+(?![A-Za-z0-9])",
        " ",
        cleaned_stem,
        flags=re.IGNORECASE,
    )
    cleaned_stem = re.sub(
        r"(?<![A-Za-z0-9])(?:fabric|forge|neoforge|quilt)(?![A-Za-z0-9])",
        " ",
        cleaned_stem,
        flags=re.IGNORECASE,
    )
    cleaned_stem = re.sub(
        r"(?<![A-Za-z0-9])(?:all|release)(?![A-Za-z0-9])",
        " ",
        cleaned_stem,
        flags=re.IGNORECASE,
    )
    cleaned_stem = re.sub(r"[-_.+]+", " ", cleaned_stem).strip()
    terms: list[str] = []
    seen: set[str] = set()
    for raw_term in (friendly_name, cleaned_stem, filename_stem):
        term = raw_term.strip()
        normalised = _normalised_project_name(term)
        if not normalised or normalised in seen:
            continue
        terms.append(term)
        seen.add(normalised)
    return tuple(terms)


def _search_match_reasons(
    *,
    title: str,
    slug: str,
    search_terms: tuple[str, ...],
    game_versions: tuple[str, ...],
    loaders: tuple[str, ...],
    game_version: str | None,
    loader: str | None,
) -> tuple[ModPageMatchConfidence, tuple[ModPageMatchReason, ...]]:
    candidate_names = {_normalised_project_name(title), _normalised_project_name(slug)}
    search_names = {_normalised_project_name(term) for term in search_terms}
    exact_name = bool(candidate_names & search_names)
    reasons: list[ModPageMatchReason] = [ModPageMatchReason.NAME]
    if game_version is not None and game_version.casefold() in {
        value.casefold() for value in game_versions
    }:
        reasons.append(ModPageMatchReason.GAME_VERSION)
    if loader is not None and loader.casefold() in {value.casefold() for value in loaders}:
        reasons.append(ModPageMatchReason.LOADER)
    confidence = ModPageMatchConfidence.STRONG if exact_name else ModPageMatchConfidence.POSSIBLE
    return confidence, tuple(reasons)


def _mod_page_candidate_sort_key(candidate: ModPageCandidate) -> tuple[int, int]:
    confidence_rank = {
        ModPageMatchConfidence.EXACT: 0,
        ModPageMatchConfidence.STRONG: 1,
        ModPageMatchConfidence.POSSIBLE: 2,
    }[candidate.confidence]
    return confidence_rank, -len(candidate.match_reasons)


def _modrinth_project_candidate(
    project: Mapping[str, object],
    *,
    confidence: ModPageMatchConfidence,
    match_reasons: tuple[ModPageMatchReason, ...],
) -> ModPageCandidate:
    project_id = _required_text(project, "project_id", label="Modrinth project")
    slug = _required_text(project, "slug", label="Modrinth project")
    project_type = _optional_text(project.get("project_type")) or "mod"
    title = _required_text(project, "title", label="Modrinth project")
    return ModPageCandidate(
        provider=Provider.MODRINTH,
        page=ModPageLink(
            name=KnownModPageProvider.MODRINTH.value,
            url=f"https://modrinth.com/{quote(project_type, safe='')}/{quote(slug, safe='')}",
        ),
        project_id=project_id,
        title=title,
        author=_optional_text(project.get("author")),
        summary=_optional_text(project.get("description")),
        game_versions=(
            _text_tuple(project.get("game_versions"))
            or _text_tuple(project.get("versions"))
        ),
        loaders=(
            _text_tuple(project.get("loaders"))
            or _text_tuple(project.get("categories"))
        ),
        confidence=confidence,
        match_reasons=match_reasons,
    )


async def _modrinth_exact_project_candidate(
    local: _LocalFileIdentity,
    *,
    http: httpx.AsyncClient,
) -> ModPageCandidate | None:
    version_response = await http.get(
        f"https://api.modrinth.com/v2/version_file/{local.sha1}",
        params={"algorithm": "sha1"},
        timeout=30,
    )
    if version_response.status_code == 404:
        return None
    version_response.raise_for_status()
    version = _required_mapping(cast(object, version_response.json()), label="Modrinth version")
    project_id = _required_text(version, "project_id", label="Modrinth version")
    project_response = await http.get(
        f"https://api.modrinth.com/v2/project/{quote(project_id, safe='')}",
        timeout=30,
    )
    project_response.raise_for_status()
    project = dict(
        _required_mapping(cast(object, project_response.json()), label="Modrinth project")
    )
    project.setdefault("project_id", project_id)
    return _modrinth_project_candidate(
        project,
        confidence=ModPageMatchConfidence.EXACT,
        match_reasons=(ModPageMatchReason.FILE_HASH,),
    )


async def _search_modrinth_projects(
    *,
    search_terms: tuple[str, ...],
    game_version: str | None,
    loader: str | None,
    http: httpx.AsyncClient,
) -> tuple[ModPageCandidate, ...]:
    facets: list[list[str]] = [["project_type:mod"]]
    raw_hits: dict[str, object] = {}
    for search_term in search_terms:
        response = await http.get(
            "https://api.modrinth.com/v2/search",
            params={
                "query": search_term,
                "facets": json.dumps(facets, separators=(",", ":")),
                "index": "relevance",
                "limit": 10,
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = _required_mapping(cast(object, response.json()), label="Modrinth search")
        raw_results = payload.get("hits")
        if not isinstance(raw_results, list):
            raise ValueError("Modrinth returned invalid project search results.")
        exact_name_found = False
        for raw_hit in cast(list[object], raw_results):
            hit = _required_mapping(raw_hit, label="Modrinth search result")
            project_id = _required_text(hit, "project_id", label="Modrinth search result")
            raw_hits.setdefault(project_id, raw_hit)
            candidate_names = {
                _normalised_project_name(_required_text(hit, "title", label="Modrinth search result")),
                _normalised_project_name(_required_text(hit, "slug", label="Modrinth search result")),
            }
            if _normalised_project_name(search_term) in candidate_names:
                exact_name_found = True
        if exact_name_found:
            break
    candidates: list[ModPageCandidate] = []
    for raw_hit in raw_hits.values():
        hit = _required_mapping(raw_hit, label="Modrinth search result")
        title = _required_text(hit, "title", label="Modrinth search result")
        slug = _required_text(hit, "slug", label="Modrinth search result")
        game_versions = _text_tuple(hit.get("versions"))
        loaders = _text_tuple(hit.get("categories"))
        confidence, reasons = _search_match_reasons(
            title=title,
            slug=slug,
            search_terms=search_terms,
            game_versions=game_versions,
            loaders=loaders,
            game_version=game_version,
            loader=loader,
        )
        candidates.append(
            _modrinth_project_candidate(
                hit,
                confidence=confidence,
                match_reasons=reasons,
            )
        )
    return tuple(sorted(candidates, key=_mod_page_candidate_sort_key))


def _curseforge_project_page_url(project: Mapping[str, object]) -> str:
    raw_links = project.get("links")
    links = (
        _required_mapping(raw_links, label="CurseForge project links")
        if raw_links is not None
        else {}
    )
    website_url = _optional_text(links.get("websiteUrl"))
    if website_url is not None:
        if known_mod_page_provider_for_url(website_url) is not KnownModPageProvider.CURSEFORGE:
            raise ValueError("CurseForge returned an invalid project page URL.")
        return website_url
    slug = _required_text(project, "slug", label="CurseForge project")
    return f"https://www.curseforge.com/minecraft/mc-mods/{quote(slug, safe='')}"


def _curseforge_project_compatibility(
    project: Mapping[str, object],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    game_versions: list[str] = []
    raw_indexes = project.get("latestFilesIndexes")
    if isinstance(raw_indexes, list):
        for raw_index in cast(list[object], raw_indexes):
            index = _required_mapping(raw_index, label="CurseForge latest file index")
            game_version = _optional_text(index.get("gameVersion"))
            if game_version is not None and game_version not in game_versions:
                game_versions.append(game_version)
    category_names: list[str] = []
    raw_categories = project.get("categories")
    if isinstance(raw_categories, list):
        for raw_category in cast(list[object], raw_categories):
            category = _required_mapping(raw_category, label="CurseForge category")
            name = _optional_text(category.get("name"))
            if name is not None:
                category_names.append(name)
    known_loaders = frozenset({"forge", "fabric", "quilt", "neoforge", "liteloader", "cauldron"})
    loaders = tuple(name for name in category_names if name.casefold() in known_loaders)
    return tuple(game_versions), loaders


def _curseforge_project_candidate(
    project: Mapping[str, object],
    *,
    confidence: ModPageMatchConfidence,
    match_reasons: tuple[ModPageMatchReason, ...],
) -> ModPageCandidate:
    project_id = _required_positive_int(project, "id", label="CurseForge project")
    title = _required_text(project, "name", label="CurseForge project")
    game_versions, loaders = _curseforge_project_compatibility(project)
    author: str | None = None
    raw_authors = project.get("authors")
    if isinstance(raw_authors, list) and raw_authors:
        author = _optional_text(
            _required_mapping(raw_authors[0], label="CurseForge author").get("name")
        )
    return ModPageCandidate(
        provider=Provider.CURSEFORGE,
        page=ModPageLink(
            name=KnownModPageProvider.CURSEFORGE.value,
            url=_curseforge_project_page_url(project),
        ),
        project_id=str(project_id),
        title=title,
        author=author,
        summary=_optional_text(project.get("summary")),
        game_versions=game_versions,
        loaders=loaders,
        confidence=confidence,
        match_reasons=match_reasons,
    )


async def _curseforge_projects_by_id(
    project_ids: tuple[int, ...],
    *,
    confidence: ModPageMatchConfidence,
    match_reasons: tuple[ModPageMatchReason, ...],
    http: httpx.AsyncClient,
    api_key: str,
) -> tuple[ModPageCandidate, ...]:
    async def fetch(project_id: int) -> ModPageCandidate:
        response = await http.get(
            f"https://api.curseforge.com/v1/mods/{project_id}",
            headers={"x-api-key": api_key},
            timeout=30,
        )
        response.raise_for_status()
        payload = _required_mapping(cast(object, response.json()), label="CurseForge project response")
        project = _required_mapping(payload.get("data"), label="CurseForge project")
        return _curseforge_project_candidate(
            project,
            confidence=confidence,
            match_reasons=match_reasons,
        )

    return tuple(await asyncio.gather(*(fetch(project_id) for project_id in project_ids)))


async def _curseforge_exact_project_candidates(
    local: _LocalFileIdentity,
    *,
    http: httpx.AsyncClient,
    api_key: str,
) -> tuple[ModPageCandidate, ...]:
    response = await http.post(
        "https://api.curseforge.com/v1/fingerprints",
        headers={"x-api-key": api_key},
        json={"fingerprints": [local.curseforge_fingerprint]},
        timeout=30,
    )
    response.raise_for_status()
    payload = _required_mapping(cast(object, response.json()), label="CurseForge fingerprint response")
    data = _required_mapping(payload.get("data"), label="CurseForge fingerprint data")
    raw_matches = data.get("exactMatches")
    if not isinstance(raw_matches, list):
        raise ValueError("CurseForge returned invalid fingerprint matches.")
    project_ids: list[int] = []
    for raw_match in cast(list[object], raw_matches):
        match = _required_mapping(raw_match, label="CurseForge fingerprint match")
        file_payload = _required_mapping(match.get("file"), label="CurseForge fingerprint file")
        project_id = _required_positive_int(file_payload, "modId", label="CurseForge fingerprint file")
        if project_id not in project_ids:
            project_ids.append(project_id)
    return await _curseforge_projects_by_id(
        tuple(project_ids),
        confidence=ModPageMatchConfidence.EXACT,
        match_reasons=(ModPageMatchReason.FILE_FINGERPRINT,),
        http=http,
        api_key=api_key,
    )


async def _search_curseforge_projects(
    *,
    search_terms: tuple[str, ...],
    game_version: str | None,
    loader: str | None,
    http: httpx.AsyncClient,
    api_key: str,
) -> tuple[ModPageCandidate, ...]:
    raw_projects: list[object] = []
    for search_term in search_terms:
        response = await http.get(
            "https://api.curseforge.com/v1/mods/search",
            headers={"x-api-key": api_key},
            params={
                "gameId": 432,
                "searchFilter": search_term,
                "pageSize": 10,
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = _required_mapping(cast(object, response.json()), label="CurseForge search response")
        raw_results = payload.get("data")
        if not isinstance(raw_results, list):
            raise ValueError("CurseForge returned invalid project search results.")
        raw_projects = cast(list[object], raw_results)
        if raw_projects:
            break
    candidates: list[ModPageCandidate] = []
    for raw_project in raw_projects:
        project = _required_mapping(raw_project, label="CurseForge search result")
        title = _required_text(project, "name", label="CurseForge search result")
        slug = _required_text(project, "slug", label="CurseForge search result")
        game_versions, loaders = _curseforge_project_compatibility(project)
        confidence, reasons = _search_match_reasons(
            title=title,
            slug=slug,
            search_terms=search_terms,
            game_versions=game_versions,
            loaders=loaders,
            game_version=game_version,
            loader=loader,
        )
        candidates.append(
            _curseforge_project_candidate(
                project,
                confidence=confidence,
                match_reasons=reasons,
            )
        )
    return tuple(sorted(candidates, key=_mod_page_candidate_sort_key))


async def _bulk_local_file_identities(
    targets: tuple[BulkLauncherMetadataTarget, ...],
) -> dict[str, _LocalFileIdentity]:
    log.info("Bulk launcher metadata identity scan started: files=%s", len(targets))
    semaphore = asyncio.Semaphore(4)

    async def calculate(target: BulkLauncherMetadataTarget) -> tuple[str, _LocalFileIdentity]:
        async with semaphore:
            identity = await asyncio.to_thread(
                _local_file_identity,
                target.local_path,
                logical_filename=target.mod_name,
            )
        return target.mod_name, identity

    identities = dict(await asyncio.gather(*(calculate(target) for target in targets)))
    log.info("Bulk launcher metadata identity scan completed: files=%s", len(identities))
    return identities


async def _bulk_modrinth_exact_matches(
    targets: tuple[BulkLauncherMetadataTarget, ...],
    identities: Mapping[str, _LocalFileIdentity],
    *,
    http: httpx.AsyncClient,
) -> dict[str, _BulkModrinthMatch]:
    eligible_targets = tuple(
        target for target in targets if target.existing_platforms.modrinth is None
    )
    if not eligible_targets:
        return {}
    hashes = tuple(dict.fromkeys(identities[target.mod_name].sha1 for target in eligible_targets))
    log.info("Bulk Modrinth file lookup started: hashes=%s", len(hashes))
    response = await http.post(
        "https://api.modrinth.com/v2/version_files",
        json={"hashes": hashes, "algorithm": "sha1"},
        timeout=30,
    )
    response.raise_for_status()
    versions_by_hash = _required_mapping(
        cast(object, response.json()),
        label="Modrinth bulk version response",
    )
    project_ids = tuple(
        dict.fromkeys(
            _required_text(
                _required_mapping(raw_version, label="Modrinth version"),
                "project_id",
                label="Modrinth version",
            )
            for raw_version in versions_by_hash.values()
        )
    )
    projects_by_id: dict[str, Mapping[str, object]] = {}
    for offset in range(0, len(project_ids), 100):
        project_id_chunk = project_ids[offset : offset + 100]
        projects_response = await http.get(
            "https://api.modrinth.com/v2/projects",
            params={"ids": json.dumps(project_id_chunk, separators=(",", ":"))},
            timeout=30,
        )
        projects_response.raise_for_status()
        raw_projects = cast(object, projects_response.json())
        if not isinstance(raw_projects, list):
            raise ValueError("Modrinth returned invalid bulk project metadata.")
        for raw_project in cast(list[object], raw_projects):
            project = _required_mapping(raw_project, label="Modrinth project")
            project_id = _required_text(project, "id", label="Modrinth project")
            projects_by_id[project_id] = project

    matches: dict[str, _BulkModrinthMatch] = {}
    for target in eligible_targets:
        local = identities[target.mod_name]
        raw_version = versions_by_hash.get(local.sha1)
        if raw_version is None:
            continue
        version = _required_mapping(raw_version, label="Modrinth version")
        project_id = _required_text(version, "project_id", label="Modrinth version")
        project = projects_by_id.get(project_id)
        if project is None:
            raise ValueError(f"Modrinth omitted bulk project metadata for {project_id!r}.")
        raw_files = version.get("files")
        if not isinstance(raw_files, list):
            raise ValueError("Modrinth version returned invalid file metadata.")
        selected_file: Mapping[str, object] | None = None
        for raw_file in cast(list[object], raw_files):
            file_payload = _required_mapping(raw_file, label="Modrinth file")
            hashes_payload = _required_mapping(
                file_payload.get("hashes"),
                label="Modrinth file hashes",
            )
            if _optional_text(hashes_payload.get("sha1")) == local.sha1:
                selected_file = file_payload
                break
        if selected_file is None:
            raise ValueError(f"Modrinth bulk match omitted the matching file for {target.mod_name!r}.")
        slug = _required_text(project, "slug", label="Modrinth project")
        project_type = _optional_text(project.get("project_type")) or "mod"
        project_page_url = (
            f"https://modrinth.com/{quote(project_type, safe='')}/{quote(slug, safe='')}"
        )
        version_id = _required_text(version, "id", label="Modrinth version")
        file_page_url = f"{project_page_url}/version/{quote(version_id, safe='')}"
        hashes_payload = _required_mapping(
            selected_file.get("hashes"),
            label="Modrinth file hashes",
        )
        suggested_mod_type = _suggest_mod_type_from_modrinth(
            client=_required_modrinth_side_support(project, "client_side"),
            server=_required_modrinth_side_support(project, "server_side"),
        )
        matches[target.mod_name] = _BulkModrinthMatch(
            mod_page=ModPageLink(
                name=KnownModPageProvider.MODRINTH.value,
                url=project_page_url,
            ),
            metadata=ModrinthModMetadata(
                page_url=file_page_url,
                project_id=project_id,
                version_id=version_id,
                download_url=_required_text(selected_file, "url", label="Modrinth file"),
                description=_optional_text(project.get("description")),
                filename=_required_text(selected_file, "filename", label="Modrinth file"),
                sha1=_required_text(hashes_payload, "sha1", label="Modrinth file hashes"),
                sha512=_required_text(hashes_payload, "sha512", label="Modrinth file hashes"),
                size=_required_positive_int(selected_file, "size", label="Modrinth file"),
            ),
            suggested_mod_type=suggested_mod_type,
        )
    log.info(
        "Bulk Modrinth file lookup completed: hashes=%s matches=%s projects=%s",
        len(hashes),
        len(matches),
        len(projects_by_id),
    )
    return matches


async def _bulk_curseforge_exact_matches(
    targets: tuple[BulkLauncherMetadataTarget, ...],
    identities: Mapping[str, _LocalFileIdentity],
    *,
    http: httpx.AsyncClient,
) -> dict[str, _BulkCurseForgeMatch]:
    eligible_targets = tuple(
        target for target in targets if target.existing_platforms.curseforge is None
    )
    if not eligible_targets:
        return {}
    api_key = _curseforge_api_key()
    if api_key is None:
        raise ValueError("CURSEFORGE_API_KEY is required for bulk CurseForge discovery.")
    fingerprints = tuple(
        dict.fromkeys(
            identities[target.mod_name].curseforge_fingerprint for target in eligible_targets
        )
    )
    log.info("Bulk CurseForge fingerprint lookup started: fingerprints=%s", len(fingerprints))
    response = await http.post(
        "https://api.curseforge.com/v1/fingerprints/432",
        headers={"x-api-key": api_key},
        json={"fingerprints": fingerprints},
        timeout=30,
    )
    response.raise_for_status()
    payload = _required_mapping(cast(object, response.json()), label="CurseForge fingerprint response")
    data = _required_mapping(payload.get("data"), label="CurseForge fingerprint data")
    raw_matches = data.get("exactMatches")
    if not isinstance(raw_matches, list):
        raise ValueError("CurseForge returned invalid exact fingerprint matches.")
    files_by_fingerprint: dict[int, Mapping[str, object]] = {}
    for raw_match in cast(list[object], raw_matches):
        match = _required_mapping(raw_match, label="CurseForge fingerprint match")
        file_payload = _required_mapping(match.get("file"), label="CurseForge fingerprint file")
        raw_fingerprint = file_payload.get("fileFingerprint", match.get("id"))
        if isinstance(raw_fingerprint, bool) or not isinstance(raw_fingerprint, int):
            raise ValueError("CurseForge fingerprint match omitted its fingerprint.")
        files_by_fingerprint[raw_fingerprint] = file_payload

    project_ids = tuple(
        dict.fromkeys(
            _required_positive_int(file_payload, "modId", label="CurseForge fingerprint file")
            for file_payload in files_by_fingerprint.values()
        )
    )
    projects_by_id: dict[int, Mapping[str, object]] = {}
    for offset in range(0, len(project_ids), 50):
        project_id_chunk = project_ids[offset : offset + 50]
        projects_response = await http.post(
            "https://api.curseforge.com/v1/mods",
            headers={"x-api-key": api_key},
            json={"modIds": project_id_chunk},
            timeout=30,
        )
        projects_response.raise_for_status()
        projects_payload = _required_mapping(
            cast(object, projects_response.json()),
            label="CurseForge bulk projects response",
        )
        raw_projects = projects_payload.get("data")
        if not isinstance(raw_projects, list):
            raise ValueError("CurseForge returned invalid bulk project metadata.")
        for raw_project in cast(list[object], raw_projects):
            project = _required_mapping(raw_project, label="CurseForge project")
            project_id = _required_positive_int(project, "id", label="CurseForge project")
            projects_by_id[project_id] = project

    matches: dict[str, _BulkCurseForgeMatch] = {}
    for target in eligible_targets:
        local = identities[target.mod_name]
        file_payload = files_by_fingerprint.get(local.curseforge_fingerprint)
        if file_payload is None:
            continue
        project_id = _required_positive_int(
            file_payload,
            "modId",
            label="CurseForge fingerprint file",
        )
        project = projects_by_id.get(project_id)
        if project is None:
            raise ValueError(f"CurseForge omitted bulk project metadata for {project_id}.")
        project_page_url = _curseforge_project_page_url(project)
        file_id = _required_positive_int(file_payload, "id", label="CurseForge fingerprint file")
        file_page_url = f"{project_page_url}/files/{file_id}"
        matches[target.mod_name] = _BulkCurseForgeMatch(
            mod_page=ModPageLink(
                name=KnownModPageProvider.CURSEFORGE.value,
                url=project_page_url,
            ),
            metadata=CurseForgeModMetadata(
                page_url=file_page_url,
                project_id=project_id,
                file_id=file_id,
                description=_optional_text(project.get("summary")),
            ),
        )
    log.info(
        "Bulk CurseForge fingerprint lookup completed: fingerprints=%s matches=%s projects=%s",
        len(fingerprints),
        len(matches),
        len(projects_by_id),
    )
    return matches


async def discover_bulk_launcher_metadata(
    *,
    scope: str,
    targets: tuple[BulkLauncherMetadataTarget, ...],
    http: httpx.AsyncClient | None = None,
) -> BulkLauncherMetadataDiscovery:
    capabilities = mod_capabilities_for_scope(scope)
    supported = frozenset(capabilities.launcher_metadata_providers)
    if not supported:
        raise ValueError(f"{scope} does not support bulk launcher metadata discovery.")
    mod_names = tuple(target.mod_name for target in targets)
    if len(mod_names) != len(set(mod_names)):
        raise ValueError("Bulk launcher metadata targets must have unique mod names.")
    if not targets:
        return BulkLauncherMetadataDiscovery()

    identity_targets = tuple(
        target
        for target in targets
        if target.existing_platforms.modrinth is None
        or target.existing_platforms.curseforge is None
    )
    identities = await _bulk_local_file_identities(identity_targets)
    owned_http = http is None
    client = http or httpx.AsyncClient()
    modrinth_matches: dict[str, _BulkModrinthMatch] = {}
    curseforge_matches: dict[str, _BulkCurseForgeMatch] = {}
    provider_errors: list[LauncherMetadataProviderError] = []

    async def discover_modrinth() -> None:
        nonlocal modrinth_matches
        if Provider.MODRINTH not in supported:
            return
        try:
            modrinth_matches = await _bulk_modrinth_exact_matches(
                targets,
                identities,
                http=client,
            )
        except (ValueError, httpx.HTTPError) as xcp:
            provider_errors.append(_metadata_provider_error(Provider.MODRINTH, xcp))

    async def discover_curseforge() -> None:
        nonlocal curseforge_matches
        if Provider.CURSEFORGE not in supported:
            return
        try:
            curseforge_matches = await _bulk_curseforge_exact_matches(
                targets,
                identities,
                http=client,
            )
        except (ValueError, httpx.HTTPError) as xcp:
            provider_errors.append(_metadata_provider_error(Provider.CURSEFORGE, xcp))

    try:
        await asyncio.gather(discover_modrinth(), discover_curseforge())
    finally:
        if owned_http:
            await client.aclose()

    entries: list[BulkLauncherMetadataEntry] = []
    for target in targets:
        modrinth_match = modrinth_matches.get(target.mod_name)
        curseforge_match = curseforge_matches.get(target.mod_name)
        existing_page_providers = {
            provider
            for page in target.existing_mod_pages
            if (provider := known_mod_page_provider_for_url(page.url)) is not None
        }
        if (
            modrinth_match is None
            and target.existing_platforms.modrinth is not None
            and KnownModPageProvider.MODRINTH not in existing_page_providers
        ):
            existing_modrinth = target.existing_platforms.modrinth
            modrinth_match = _BulkModrinthMatch(
                mod_page=ModPageLink(
                    name=KnownModPageProvider.MODRINTH.value,
                    url=launcher_project_page_url(
                        existing_modrinth.page_url,
                        Provider.MODRINTH,
                    ),
                ),
                metadata=existing_modrinth,
                suggested_mod_type=None,
            )
        if (
            curseforge_match is None
            and target.existing_platforms.curseforge is not None
            and target.existing_platforms.curseforge.page_url is not None
            and KnownModPageProvider.CURSEFORGE not in existing_page_providers
        ):
            existing_curseforge = target.existing_platforms.curseforge
            assert existing_curseforge.page_url is not None
            curseforge_match = _BulkCurseForgeMatch(
                mod_page=ModPageLink(
                    name=KnownModPageProvider.CURSEFORGE.value,
                    url=launcher_project_page_url(
                        existing_curseforge.page_url,
                        Provider.CURSEFORGE,
                    ),
                ),
                metadata=existing_curseforge,
            )
        matched_providers = tuple(
            provider
            for provider, match in (
                (Provider.MODRINTH, modrinth_match),
                (Provider.CURSEFORGE, curseforge_match),
            )
            if match is not None
        )
        proposed_pages = tuple(
            match.mod_page
            for known_provider, match in (
                (KnownModPageProvider.MODRINTH, modrinth_match),
                (KnownModPageProvider.CURSEFORGE, curseforge_match),
            )
            if match is not None and known_provider not in existing_page_providers
        )
        entries.append(
            BulkLauncherMetadataEntry(
                mod_name=target.mod_name,
                friendly_name=target.friendly_name,
                status=(
                    BulkLauncherMetadataStatus.EXACT
                    if matched_providers
                    else BulkLauncherMetadataStatus.UNMATCHED
                ),
                mod_pages=proposed_pages,
                platforms=ModPlatformMetadata(
                    modrinth=None if modrinth_match is None else modrinth_match.metadata,
                    curseforge=None if curseforge_match is None else curseforge_match.metadata,
                ),
                suggested_mod_type=(
                    None if modrinth_match is None else modrinth_match.suggested_mod_type
                ),
                matched_providers=matched_providers,
            )
        )
    return BulkLauncherMetadataDiscovery(
        entries=tuple(entries),
        provider_errors=tuple(sorted(provider_errors, key=lambda error: error.provider.value)),
    )


async def discover_mod_pages(
    *,
    scope: str,
    existing_mod_pages: tuple[ModPageLink, ...],
    local_path: Path,
    local_filename: str | None = None,
    friendly_name: str,
    detected_version: str | None = None,
    game_version: str | None = None,
    loader: str | None = None,
    providers: tuple[Provider, ...] | None = None,
    http: httpx.AsyncClient | None = None,
) -> ModPageDiscovery:
    capabilities = mod_capabilities_for_scope(scope)
    selected_providers = _selected_launcher_metadata_providers(
        scope=scope,
        supported=capabilities.launcher_metadata_providers,
        requested=providers,
    )
    existing_providers = {
        provider
        for page in existing_mod_pages
        if (provider := known_mod_page_provider_for_url(page.url)) is not None
    }
    launcher_to_known = {
        Provider.MODRINTH: KnownModPageProvider.MODRINTH,
        Provider.CURSEFORGE: KnownModPageProvider.CURSEFORGE,
    }
    unresolved_providers = tuple(
        provider
        for provider in selected_providers
        if launcher_to_known.get(provider) not in existing_providers
    )
    if not unresolved_providers:
        raise ValueError("Mod Pages already contains all supported project providers.")

    local = await asyncio.to_thread(
        _local_file_identity,
        local_path,
        logical_filename=local_filename,
    )
    search_terms = _project_search_terms(
        friendly_name=friendly_name,
        local_filename=local.filename,
        detected_version=detected_version,
        game_version=game_version,
        loader=loader,
    )
    if not search_terms:
        raise ValueError("No usable local mod name was found for project search.")
    owned_http = http is None
    client = http or httpx.AsyncClient()

    async def discover_provider(provider: Provider) -> ModPageProviderCandidates:
        try:
            match provider:
                case Provider.MODRINTH:
                    exact = await _modrinth_exact_project_candidate(local, http=client)
                    candidates = (
                        (exact,)
                        if exact is not None
                        else await _search_modrinth_projects(
                            search_terms=search_terms,
                            game_version=game_version,
                            loader=loader,
                            http=client,
                        )
                    )
                case Provider.CURSEFORGE:
                    api_key = _curseforge_api_key()
                    if api_key is None:
                        raise ValueError("CURSEFORGE_API_KEY is required to find CurseForge projects.")
                    exact_candidates = await _curseforge_exact_project_candidates(
                        local,
                        http=client,
                        api_key=api_key,
                    )
                    candidates = (
                        exact_candidates
                        if exact_candidates
                        else await _search_curseforge_projects(
                            search_terms=search_terms,
                            game_version=game_version,
                            loader=loader,
                            http=client,
                            api_key=api_key,
                        )
                    )
                case _:
                    raise ValueError(f"Unsupported mod page provider: {provider.value}")
        except (ValueError, httpx.HTTPError) as xcp:
            return ModPageProviderCandidates(provider=provider, error=str(xcp))
        return ModPageProviderCandidates(provider=provider, candidates=candidates)

    try:
        results = await asyncio.gather(
            *(discover_provider(provider) for provider in unresolved_providers)
        )
        return ModPageDiscovery(providers=tuple(results))
    finally:
        if owned_http:
            await client.aclose()


async def discover_launcher_metadata(
    *,
    scope: str,
    mod_pages: tuple[ModPageLink, ...],
    existing_urls: LauncherProviderUrls,
    local_path: Path,
    local_filename: str | None = None,
    game_version: str | None = None,
    loader: str | None = None,
    providers: tuple[Provider, ...] | None = None,
    http: httpx.AsyncClient | None = None,
) -> LauncherMetadataDiscovery:
    capabilities = mod_capabilities_for_scope(scope)
    selected_providers = _selected_launcher_metadata_providers(
        scope=scope,
        supported=capabilities.launcher_metadata_providers,
        requested=providers,
    )
    supported = frozenset(selected_providers)
    excluded = frozenset(provider for provider in supported if existing_urls.has_provider(provider))
    page_collection = _collect_launcher_project_pages(
        mod_pages,
        supported=supported,
        excluded=excluded,
    )
    if not page_collection.project_pages:
        if page_collection.errors:
            return LauncherMetadataDiscovery(
                providers=tuple(
                    page_collection.errors[provider]
                    for provider in selected_providers
                    if provider in page_collection.errors
                )
            )
        raise ValueError(
            "No unresolved Modrinth or CurseForge project pages were found in Mod Pages."
        )

    local = await asyncio.to_thread(
        _local_file_identity,
        local_path,
        logical_filename=local_filename,
    )
    owned_http = http is None
    client = http or httpx.AsyncClient()

    async def discover_provider(
        provider: Provider,
        project_page_url: str,
    ) -> LauncherMetadataProviderCandidates:
        try:
            match provider:
                case Provider.MODRINTH:
                    candidates = await _discover_modrinth_candidates(
                        project_page_url,
                        local=local,
                        game_version=game_version,
                        loader=loader,
                        http=client,
                    )
                case Provider.CURSEFORGE:
                    candidates = await _discover_curseforge_candidates(
                        project_page_url,
                        local=local,
                        game_version=game_version,
                        loader=loader,
                        http=client,
                    )
                case _:
                    raise ValueError(f"Unsupported launcher metadata provider: {provider.value}")
            candidates = _candidates_for_explicit_file_page(
                candidates,
                provider=provider,
                file_page_url=page_collection.explicit_file_pages.get(provider),
            )
        except (ValueError, httpx.HTTPError) as xcp:
            return LauncherMetadataProviderCandidates(
                provider=provider,
                project_page_url=project_page_url,
                error=str(xcp),
            )
        return LauncherMetadataProviderCandidates(
            provider=provider,
            project_page_url=project_page_url,
            candidates=candidates,
        )

    try:
        provider_results = await asyncio.gather(
            *(
                discover_provider(provider, project_page_url)
                for provider, project_page_url in page_collection.project_pages.items()
            )
        )
        results_by_provider = {
            **page_collection.errors,
            **{result.provider: result for result in provider_results},
        }
        return LauncherMetadataDiscovery(
            providers=tuple(
                results_by_provider[provider]
                for provider in selected_providers
                if provider in results_by_provider
            )
        )
    finally:
        if owned_http:
            await client.aclose()


async def resolve_launcher_metadata_resolution(
    *,
    scope: str,
    urls: LauncherProviderUrls,
    local_filename: str,
    local_path: Path | None = None,
    providers: tuple[Provider, ...] | None = None,
    http: httpx.AsyncClient | None = None,
) -> LauncherMetadataResolution:
    capabilities = mod_capabilities_for_scope(scope)
    selected_providers = _selected_launcher_metadata_providers(
        scope=scope,
        supported=capabilities.launcher_metadata_providers,
        requested=providers,
    )
    supported = frozenset(selected_providers)
    app_supported = frozenset(capabilities.launcher_metadata_providers)
    all_supplied_providers = {
        provider
        for provider in (Provider.MODRINTH, Provider.CURSEFORGE)
        if urls.has_provider(provider)
    }
    unsupported = tuple(
        provider for provider in all_supplied_providers if provider not in app_supported
    )
    if unsupported:
        names = ", ".join(launcher_provider_label(provider) for provider in unsupported)
        raise ValueError(f"{scope} does not support launcher metadata from: {names}.")
    supplied_urls = {
        provider: page_url
        for provider in (Provider.MODRINTH, Provider.CURSEFORGE)
        if provider in supported and (page_url := urls.for_provider(provider)) is not None
    }
    supplied_providers = {
        provider for provider in supported if urls.has_provider(provider)
    }
    if providers is not None and not supplied_providers:
        names = ", ".join(launcher_provider_label(provider) for provider in selected_providers)
        raise ValueError(f"No launcher metadata source was supplied for: {names}.")

    owned_http = http is None
    client = http or httpx.AsyncClient()
    try:
        local_sha1 = (
            None
            if local_path is None or Provider.MODRINTH not in supplied_urls
            else (
                await asyncio.to_thread(
                    _local_file_identity,
                    local_path,
                    logical_filename=local_filename,
                )
            ).sha1
        )
        modrinth: ModrinthModMetadata | None = None
        curseforge: CurseForgeModMetadata | None = None
        suggested_mod_type: ModType | None = None
        provider_errors: list[LauncherMetadataProviderError] = []

        async def resolve_modrinth_provider() -> None:
            nonlocal modrinth, suggested_mod_type
            try:
                modrinth, suggested_mod_type = await _resolve_modrinth(
                    supplied_urls[Provider.MODRINTH],
                    local_filename=local_filename,
                    local_sha1=local_sha1,
                    http=client,
                )
            except (ValueError, httpx.HTTPError) as xcp:
                provider_errors.append(_metadata_provider_error(Provider.MODRINTH, xcp))

        async def resolve_curseforge_provider() -> None:
            nonlocal curseforge
            try:
                curseforge = await _resolve_curseforge_source(urls, http=client)
            except (ValueError, httpx.HTTPError) as xcp:
                provider_errors.append(_metadata_provider_error(Provider.CURSEFORGE, xcp))

        provider_resolutions: list[Awaitable[None]] = []
        if Provider.MODRINTH in supplied_urls:
            provider_resolutions.append(resolve_modrinth_provider())
        if Provider.CURSEFORGE in supplied_providers:
            provider_resolutions.append(resolve_curseforge_provider())
        await asyncio.gather(*provider_resolutions)

        provider_errors.sort(key=lambda error: error.provider.value)
        if provider_errors and modrinth is None and curseforge is None:
            details = "; ".join(
                f"{launcher_provider_label(error.provider)}: {error.message}"
                for error in provider_errors
            )
            raise ValueError(f"Launcher metadata lookup failed: {details}")
        for error in provider_errors:
            log.warning(
                "%s launcher metadata lookup failed; other providers will be retained: %s",
                launcher_provider_label(error.provider),
                error.message,
            )
        return LauncherMetadataResolution(
            platforms=ModPlatformMetadata(modrinth=modrinth, curseforge=curseforge),
            suggested_mod_type=suggested_mod_type,
            suggestion_provider=(Provider.MODRINTH if suggested_mod_type is not None else None),
            provider_errors=tuple(provider_errors),
        )
    finally:
        if owned_http:
            await client.aclose()


async def resolve_launcher_metadata(
    *,
    scope: str,
    urls: LauncherProviderUrls,
    local_filename: str,
    local_path: Path | None = None,
    providers: tuple[Provider, ...] | None = None,
    http: httpx.AsyncClient | None = None,
) -> ModPlatformMetadata:
    resolution = await resolve_launcher_metadata_resolution(
        scope=scope,
        urls=urls,
        local_filename=local_filename,
        local_path=local_path,
        providers=providers,
        http=http,
    )
    return resolution.platforms

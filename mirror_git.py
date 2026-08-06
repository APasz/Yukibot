"""Git-provider transport for public update-mirror sources."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Final

from mirror_models import (
    GitMirrorSource,
    GitReferenceOption,
    MirrorArchiveLimits,
    MirrorError,
    MirrorGitHost,
    MirrorTrackingMode,
    format_byte_count,
    normalise_git_reference,
    normalise_repository_identifier,
)

_HTTP_TIMEOUT_SECONDS: Final[float] = 30.0
_HTTP_USER_AGENT: Final[str] = "YukibotMirror/1.0"
_MAX_BRANCH_REFERENCE_OPTIONS: Final[int] = 100
_MAX_COMMIT_REFERENCE_OPTIONS: Final[int] = 50


class GitProviderClient:
    """Fetch validated metadata, revisions, and archives from supported providers."""

    def __init__(self, *, archive_limits: MirrorArchiveLimits) -> None:
        self._archive_limits = archive_limits

    def fetch_repository_metadata(self, *, host: MirrorGitHost, repository: str) -> dict[str, object]:
        return self.fetch_json(self._repository_metadata_url(host=host, repository=repository))

    def list_reference_options(
        self,
        *,
        host: MirrorGitHost,
        repository: str,
        tracking_mode: MirrorTrackingMode,
    ) -> tuple[GitReferenceOption, ...]:
        records = self.fetch_json_list(
            self._reference_records_url(host=host, repository=repository, tracking_mode=tracking_mode)
        )
        options: list[GitReferenceOption] = []
        seen_references: set[str] = set()
        for record in records:
            option = self._reference_option(host=host, record=record, tracking_mode=tracking_mode)
            if option is None or option.ref in seen_references:
                continue
            seen_references.add(option.ref)
            options.append(option)
        if not options:
            reference_kind = "branches" if tracking_mode is MirrorTrackingMode.BRANCH else "commits"
            raise MirrorError(f"Git provider did not return any supported {reference_kind}.")
        return tuple(options)

    def resolve_revision(self, *, host: MirrorGitHost, repository: str, ref: str) -> str:
        payload = self.fetch_json(self._commit_url(host=host, repository=repository, ref=ref))
        revision_key = "sha" if host is MirrorGitHost.GITHUB else "id"
        revision = payload.get(revision_key)
        try:
            return normalise_git_reference(str(revision), tracking_mode=MirrorTrackingMode.PINNED_COMMIT)
        except MirrorError as xcp:
            raise MirrorError("Git provider returned an invalid commit revision.") from xcp

    def download_archive(self, *, source: GitMirrorSource, revision: str) -> bytes:
        return self.download_bytes(self._archive_url(source=source, revision=revision))

    def fetch_json(self, url: str) -> dict[str, object]:
        value = self._fetch_json_value(url)
        if not isinstance(value, dict):
            raise MirrorError("Git provider returned an unexpected response.")
        return {str(key): item for key, item in value.items()}

    def fetch_json_list(self, url: str) -> tuple[dict[str, object], ...]:
        value = self._fetch_json_value(url)
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise MirrorError("Git provider returned an unexpected response.")
        return tuple({str(key): item for key, item in record.items()} for record in value)

    def download_bytes(self, url: str) -> bytes:
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json, application/zip;q=0.9", "User-Agent": _HTTP_USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        declared_size = int(content_length)
                    except ValueError as xcp:
                        raise MirrorError("Git provider returned an invalid Content-Length header.") from xcp
                    if declared_size > self._archive_limits.archive_bytes:
                        raise MirrorError(
                            f"Mirror archives must not exceed {format_byte_count(self._archive_limits.archive_bytes)}."
                        )
                output = io.BytesIO()
                while chunk := response.read(self._archive_limits.copy_chunk_bytes):
                    if output.tell() + len(chunk) > self._archive_limits.archive_bytes:
                        raise MirrorError(
                            f"Mirror archives must not exceed {format_byte_count(self._archive_limits.archive_bytes)}."
                        )
                    output.write(chunk)
                return output.getvalue()
        except urllib.error.HTTPError as xcp:
            raise MirrorError(f"Git provider rejected the source ({xcp.code}).") from xcp
        except urllib.error.URLError as xcp:
            raise MirrorError(f"Could not contact Git provider: {xcp.reason}") from xcp

    def _fetch_json_value(self, url: str) -> object:
        try:
            return json.loads(self.download_bytes(url))
        except json.JSONDecodeError as xcp:
            raise MirrorError("Git provider returned invalid JSON.") from xcp

    @staticmethod
    def _repository_metadata_url(*, host: MirrorGitHost, repository: str) -> str:
        repository = normalise_repository_identifier(repository)
        if host is MirrorGitHost.GITHUB:
            return f"https://api.github.com/repos/{repository}"
        return f"https://gitlab.com/api/v4/projects/{urllib.parse.quote(repository, safe='')}"

    @staticmethod
    def _reference_records_url(
        *,
        host: MirrorGitHost,
        repository: str,
        tracking_mode: MirrorTrackingMode,
    ) -> str:
        repository = normalise_repository_identifier(repository)
        option_count = _MAX_BRANCH_REFERENCE_OPTIONS if tracking_mode is MirrorTrackingMode.BRANCH else _MAX_COMMIT_REFERENCE_OPTIONS
        if host is MirrorGitHost.GITHUB:
            path = "branches" if tracking_mode is MirrorTrackingMode.BRANCH else "commits"
            return f"https://api.github.com/repos/{repository}/{path}?per_page={option_count}"
        path = "branches" if tracking_mode is MirrorTrackingMode.BRANCH else "commits"
        encoded_repository = urllib.parse.quote(repository, safe="")
        return f"https://gitlab.com/api/v4/projects/{encoded_repository}/repository/{path}?per_page={option_count}"

    @staticmethod
    def _commit_url(*, host: MirrorGitHost, repository: str, ref: str) -> str:
        repository = normalise_repository_identifier(repository)
        encoded_reference = urllib.parse.quote(ref, safe="")
        if host is MirrorGitHost.GITHUB:
            return f"https://api.github.com/repos/{repository}/commits/{encoded_reference}"
        encoded_repository = urllib.parse.quote(repository, safe="")
        return f"https://gitlab.com/api/v4/projects/{encoded_repository}/repository/commits/{encoded_reference}"

    @staticmethod
    def _archive_url(*, source: GitMirrorSource, revision: str) -> str:
        if source.host is MirrorGitHost.GITHUB:
            return f"https://codeload.github.com/{source.repository}/zip/{revision}"
        encoded_repository = urllib.parse.quote(source.repository, safe="")
        return (
            f"https://gitlab.com/api/v4/projects/{encoded_repository}/repository/archive.zip"
            f"?sha={urllib.parse.quote(revision, safe='')}"
        )

    @staticmethod
    def _reference_option(
        *,
        host: MirrorGitHost,
        record: dict[str, object],
        tracking_mode: MirrorTrackingMode,
    ) -> GitReferenceOption | None:
        if tracking_mode is MirrorTrackingMode.BRANCH:
            reference = record.get("name")
            summary = ""
        elif host is MirrorGitHost.GITHUB:
            reference = record.get("sha")
            commit = record.get("commit")
            summary = commit.get("message", "") if isinstance(commit, dict) else ""
        else:
            reference = record.get("id")
            summary = record.get("title", "")
        if not isinstance(reference, str) or not isinstance(summary, str):
            return None
        try:
            return GitReferenceOption(tracking_mode=tracking_mode, ref=reference, summary=summary)
        except MirrorError:
            return None

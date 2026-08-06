"""Validated domain types and parsing for update mirrors."""

from __future__ import annotations

import dataclasses
import re
import urllib.parse
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

_PROJECT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
_GIT_REFERENCE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
_GIT_COMMIT_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")
_GIT_COMMIT_LINK_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{7,40}$")
_SHA256_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_REPOSITORY_PART_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class MirrorError(RuntimeError):
    """Raised when a mirror configuration, source, or snapshot is invalid."""


class MirrorRevisionUnavailable(MirrorError):
    """Raised when a requested immutable mirror snapshot is no longer retained."""


class MirrorGitHost(StrEnum):
    GITHUB = "github"
    GITLAB = "gitlab"


class MirrorSourceKind(StrEnum):
    GIT_REPOSITORY = "git_repository"
    UPLOAD_ARCHIVE = "upload_archive"


class MirrorTrackingMode(StrEnum):
    BRANCH = "branch"
    PINNED_COMMIT = "pinned_commit"


class MirrorSyncState(StrEnum):
    UNPUBLISHED = "unpublished"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    DISABLED = "disabled"


class MirrorAutoSyncOutcome(StrEnum):
    """The result of one scheduled branch check."""

    UNCHANGED = "unchanged"
    PUBLISHED = "published"
    FAILED = "failed"


@dataclasses.dataclass(frozen=True, slots=True)
class MirrorArchiveLimits:
    """Resource limits for untrusted mirror archives and provider responses."""

    archive_bytes: int = 32 * 1024 * 1024
    extracted_bytes: int = 128 * 1024 * 1024
    file_bytes: int = 16 * 1024 * 1024
    file_count: int = 4_000
    archive_member_count: int = 8_000
    copy_chunk_bytes: int = 64 * 1024
    retained_snapshot_revisions: int = 4

    def __post_init__(self) -> None:
        for field in dataclasses.fields(self):
            value = getattr(self, field.name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field.name} must be positive.")


@dataclasses.dataclass(frozen=True, slots=True)
class GitRepositoryLink:
    """A supported public provider URL and any revision encoded in it."""

    host: MirrorGitHost
    repository: str
    tracking_mode: MirrorTrackingMode | None = None
    ref: str | None = None

    def __post_init__(self) -> None:
        repository = normalise_repository_identifier(self.repository)
        if (self.tracking_mode is None) != (self.ref is None):
            raise MirrorError("Git repository links must include both a reference and tracking mode.")
        ref = self.ref
        if self.tracking_mode is MirrorTrackingMode.PINNED_COMMIT:
            if ref is None or _GIT_COMMIT_LINK_PATTERN.fullmatch(ref.casefold()) is None:
                raise MirrorError("Git commit links must contain a 7 to 40 character hexadecimal commit SHA.")
            ref = ref.casefold()
        elif self.tracking_mode is MirrorTrackingMode.BRANCH:
            if ref is None:
                raise MirrorError("Git branch links must contain a branch name.")
            ref = normalise_git_reference(ref, tracking_mode=self.tracking_mode)
        object.__setattr__(self, "repository", repository)
        object.__setattr__(self, "ref", ref)


@dataclasses.dataclass(frozen=True, slots=True)
class GitMirrorSource:
    host: MirrorGitHost
    repository: str
    tracking_mode: MirrorTrackingMode
    ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "repository", normalise_repository_identifier(self.repository))
        object.__setattr__(self, "ref", normalise_git_reference(self.ref, tracking_mode=self.tracking_mode))

    @property
    def kind(self) -> MirrorSourceKind:
        return MirrorSourceKind.GIT_REPOSITORY

    @property
    def web_url(self) -> str:
        return f"https://{host_domain(self.host)}/{self.repository}"

    def to_mapping(self) -> dict[str, str]:
        return {
            "kind": self.kind.value,
            "host": self.host.value,
            "repository": self.repository,
            "tracking_mode": self.tracking_mode.value,
            "ref": self.ref,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class GitRepositoryInspection:
    """Provider metadata used to prefill the mirror configuration form."""

    source: GitMirrorSource
    display_name: str
    suggested_project_id: str
    default_branch: str

    def __post_init__(self) -> None:
        display_name = self.display_name.strip()
        if not display_name:
            raise MirrorError("Git providers must return a repository display name.")
        object.__setattr__(self, "display_name", display_name[:80])
        object.__setattr__(self, "suggested_project_id", normalise_project_id(self.suggested_project_id))
        object.__setattr__(
            self,
            "default_branch",
            normalise_git_reference(self.default_branch, tracking_mode=MirrorTrackingMode.BRANCH),
        )


@dataclasses.dataclass(frozen=True, slots=True)
class GitReferenceOption:
    """A branch or immutable commit returned by a supported Git provider."""

    tracking_mode: MirrorTrackingMode
    ref: str
    summary: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "ref", normalise_git_reference(self.ref, tracking_mode=self.tracking_mode))
        object.__setattr__(self, "summary", " ".join(self.summary.split())[:120])

    @property
    def label(self) -> str:
        if self.tracking_mode is MirrorTrackingMode.BRANCH or not self.summary:
            return self.ref
        return f"{self.ref[:12]} · {self.summary}"


@dataclasses.dataclass(frozen=True, slots=True)
class UploadArchiveSource:
    archive_sha256: str
    original_filename: str

    def __post_init__(self) -> None:
        archive_sha256 = self.archive_sha256.strip().casefold()
        original_filename = Path(self.original_filename).name.strip()
        if _SHA256_PATTERN.fullmatch(archive_sha256) is None:
            raise MirrorError("Upload archive SHA-256 must be a lowercase 64-character hexadecimal digest.")
        if not original_filename or Path(original_filename).suffix.casefold() != ".zip":
            raise MirrorError("Mirror uploads must be ZIP archives.")
        object.__setattr__(self, "archive_sha256", archive_sha256)
        object.__setattr__(self, "original_filename", original_filename)

    @property
    def kind(self) -> MirrorSourceKind:
        return MirrorSourceKind.UPLOAD_ARCHIVE

    def to_mapping(self) -> dict[str, str]:
        return {
            "kind": self.kind.value,
            "archive_sha256": self.archive_sha256,
            "original_filename": self.original_filename,
        }


MirrorSource = GitMirrorSource | UploadArchiveSource


@dataclasses.dataclass(frozen=True, slots=True)
class MirrorFileRecord:
    path: str
    size: int
    sha256: str

    def to_mapping(self) -> dict[str, object]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


@dataclasses.dataclass(frozen=True, slots=True)
class MirrorProject:
    project_id: str
    display_name: str
    owner_user_id: int
    source: MirrorSource
    publish_root: str = ""
    sync_state: MirrorSyncState = MirrorSyncState.UNPUBLISHED
    status_detail: str | None = None
    published_revision: str | None = None
    published_at: str | None = None
    last_checked_at: str | None = None
    next_check_at: str | None = None

    def __post_init__(self) -> None:
        project_id = normalise_project_id(self.project_id)
        display_name = self.display_name.strip()
        if not display_name or len(display_name) > 80:
            raise MirrorError("Mirror display names must contain between 1 and 80 characters.")
        if self.owner_user_id <= 0:
            raise MirrorError("Mirror owners must have a positive Discord user ID.")
        published_revision = normalise_published_revision(self.published_revision)
        published_at = normalise_timestamp(self.published_at)
        if (published_revision is None) != (published_at is None):
            raise MirrorError("Published mirror revisions and timestamps must be recorded together.")
        last_checked_at = normalise_timestamp(self.last_checked_at)
        next_check_at = normalise_timestamp(self.next_check_at)
        if not self.is_auto_sync_eligible:
            last_checked_at = None
            next_check_at = None
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "publish_root", normalise_publish_root(self.publish_root))
        object.__setattr__(self, "status_detail", normalise_status_detail(self.status_detail))
        object.__setattr__(self, "published_revision", published_revision)
        object.__setattr__(self, "published_at", published_at)
        object.__setattr__(self, "last_checked_at", last_checked_at)
        object.__setattr__(self, "next_check_at", next_check_at)

    @property
    def is_published(self) -> bool:
        return self.sync_state is MirrorSyncState.PUBLISHED and self.published_revision is not None

    @property
    def is_snapshot_available(self) -> bool:
        return self.sync_state is not MirrorSyncState.DISABLED and self.published_revision is not None

    @property
    def is_auto_sync_eligible(self) -> bool:
        return (
            self.sync_state is not MirrorSyncState.DISABLED
            and isinstance(self.source, GitMirrorSource)
            and self.source.tracking_mode is MirrorTrackingMode.BRANCH
        )

    @property
    def source_label(self) -> str:
        if isinstance(self.source, GitMirrorSource):
            return f"{self.source.host.value.title()} · {self.source.repository}"
        return f"Uploaded ZIP · {self.source.original_filename}"

    @property
    def tracking_label(self) -> str:
        if not isinstance(self.source, GitMirrorSource):
            return "Uploaded archive"
        if self.source.tracking_mode is MirrorTrackingMode.BRANCH:
            return f"Tracking {self.source.ref}"
        return f"Pinned {self.source.ref[:12]}"

    def to_mapping(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "project_id": self.project_id,
            "display_name": self.display_name,
            "owner_user_id": self.owner_user_id,
            "source": self.source.to_mapping(),
            "publish_root": self.publish_root,
            "sync_state": self.sync_state.value,
        }
        for field_name in (
            "status_detail",
            "published_revision",
            "published_at",
            "last_checked_at",
            "next_check_at",
        ):
            value = getattr(self, field_name)
            if value is not None:
                payload[field_name] = value
        return payload


@dataclasses.dataclass(frozen=True, slots=True)
class MirrorAutoSyncResult:
    """A completed scheduled source check, suitable for Portal logging."""

    project_id: str
    outcome: MirrorAutoSyncOutcome
    project: MirrorProject


def host_domain(host: MirrorGitHost) -> str:
    match host:
        case MirrorGitHost.GITHUB:
            return "github.com"
        case MirrorGitHost.GITLAB:
            return "gitlab.com"


def normalise_project_id(value: str) -> str:
    project_id = value.strip().casefold()
    if _PROJECT_ID_PATTERN.fullmatch(project_id) is None:
        raise MirrorError("Mirror IDs must use lowercase letters, numbers, and hyphens, beginning with a letter.")
    return project_id


def suggest_project_id(repository: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", repository.rsplit("/", maxsplit=1)[-1].casefold()).strip("-")
    if not slug:
        slug = "mirror"
    if not slug[0].isalpha():
        slug = f"mirror-{slug}"
    return normalise_project_id(slug[:48].rstrip("-") or "mirror")


def normalise_repository_identifier(value: str) -> str:
    repository = value.strip().strip("/")
    parts = repository.split("/")
    if len(parts) < 2 or any(_REPOSITORY_PART_PATTERN.fullmatch(part) is None for part in parts):
        raise MirrorError("Repository paths must contain valid owner/group and project segments.")
    return "/".join(parts)


def normalise_git_reference(value: str, *, tracking_mode: MirrorTrackingMode) -> str:
    reference = value.strip()
    if tracking_mode is MirrorTrackingMode.PINNED_COMMIT:
        reference = reference.casefold()
        if _GIT_COMMIT_PATTERN.fullmatch(reference) is None:
            raise MirrorError("Pinned Git revisions must be complete 40-character commit SHAs.")
        return reference
    if _GIT_REFERENCE_PATTERN.fullmatch(reference) is None or ".." in reference or reference.endswith("."):
        raise MirrorError("Git branch names contain unsupported characters.")
    return reference


def normalise_publish_root(value: str) -> str:
    root = value.strip().strip("/")
    if not root:
        return ""
    if "\\" in root or any(part in {"", ".", ".."} for part in root.split("/")):
        raise MirrorError("Publish roots must be a safe relative path using forward slashes.")
    return root


def normalise_status_detail(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip()[:500] or None


def normalise_published_revision(value: str | None) -> str | None:
    if value is None:
        return None
    revision = value.strip().casefold()
    if _GIT_COMMIT_PATTERN.fullmatch(revision) is None and _SHA256_PATTERN.fullmatch(revision) is None:
        raise MirrorError("Published revisions must be a Git commit SHA or SHA-256 digest.")
    return revision


def normalise_timestamp(value: str | None) -> str | None:
    if value is None:
        return None
    timestamp = value.strip()
    if not timestamp:
        return None
    try:
        datetime.fromisoformat(timestamp)
    except ValueError as xcp:
        raise MirrorError(f"Invalid mirror timestamp: {timestamp}") from xcp
    return timestamp


def normalise_utc_datetime(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        raise MirrorError("Mirror scheduler timestamps must include a timezone.")
    return value.astimezone(UTC)


def timestamp_as_utc_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def timestamp_from_datetime(value: datetime) -> str:
    return normalise_utc_datetime(value).isoformat(timespec="seconds")


def parse_public_git_repository_url(value: str) -> tuple[MirrorGitHost, str]:
    """Parse a canonical public GitHub or GitLab repository-root URL."""

    link = parse_public_git_repository_link(value)
    if link.tracking_mode is not None:
        raise MirrorError("Repository URL must point at the project root, not a branch or commit.")
    return (link.host, link.repository)


def parse_public_git_repository_link(value: str) -> GitRepositoryLink:
    """Parse a public provider project, branch, or commit URL without fetching it."""

    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None:
        raise MirrorError("Repository URLs must be public HTTPS GitHub or GitLab URLs.")
    try:
        port = parsed.port
    except ValueError as xcp:
        raise MirrorError("Repository URLs must use a valid HTTPS authority.") from xcp
    if parsed.query or parsed.fragment or port is not None:
        raise MirrorError("Repository URLs must not include a port, query string, or fragment.")
    hostname = parsed.hostname.casefold() if parsed.hostname is not None else ""
    host = {"github.com": MirrorGitHost.GITHUB, "gitlab.com": MirrorGitHost.GITLAB}.get(hostname)
    if host is None:
        raise MirrorError("Only github.com and gitlab.com repositories are supported.")
    parts = [urllib.parse.unquote(part) for part in parsed.path.strip("/").split("/") if part]
    if any("\x00" in part for part in parts):
        raise MirrorError("Repository URLs contain an invalid path segment.")
    return _parse_github_repository_link(parts) if host is MirrorGitHost.GITHUB else _parse_gitlab_repository_link(parts)


def project_from_mapping(value: object, *, label: str) -> MirrorProject:
    if not isinstance(value, dict):
        raise MirrorError(f"{label} must be an object.")
    try:
        sync_state = MirrorSyncState(require_string(value.get("sync_state"), label=f"{label}.sync_state"))
    except ValueError as xcp:
        raise MirrorError(f"{label}.sync_state is invalid.") from xcp
    owner_user_id = value.get("owner_user_id")
    if isinstance(owner_user_id, bool) or not isinstance(owner_user_id, int):
        raise MirrorError(f"{label}.owner_user_id must be an integer.")
    return MirrorProject(
        project_id=require_string(value.get("project_id"), label=f"{label}.project_id"),
        display_name=require_string(value.get("display_name"), label=f"{label}.display_name"),
        owner_user_id=owner_user_id,
        source=source_from_mapping(value.get("source"), label=f"{label}.source"),
        publish_root=optional_string(value.get("publish_root"), label=f"{label}.publish_root") or "",
        sync_state=sync_state,
        status_detail=optional_string(value.get("status_detail"), label=f"{label}.status_detail"),
        published_revision=optional_string(value.get("published_revision"), label=f"{label}.published_revision"),
        published_at=optional_string(value.get("published_at"), label=f"{label}.published_at"),
        last_checked_at=optional_string(value.get("last_checked_at"), label=f"{label}.last_checked_at"),
        next_check_at=optional_string(value.get("next_check_at"), label=f"{label}.next_check_at"),
    )


def source_from_mapping(value: object, *, label: str) -> MirrorSource:
    if not isinstance(value, dict):
        raise MirrorError(f"{label} must be an object.")
    try:
        kind = MirrorSourceKind(require_string(value.get("kind"), label=f"{label}.kind"))
    except ValueError as xcp:
        raise MirrorError(f"{label}.kind is invalid.") from xcp
    if kind is MirrorSourceKind.GIT_REPOSITORY:
        try:
            host = MirrorGitHost(require_string(value.get("host"), label=f"{label}.host"))
            tracking_mode = MirrorTrackingMode(require_string(value.get("tracking_mode"), label=f"{label}.tracking_mode"))
        except ValueError as xcp:
            raise MirrorError(f"{label} has an invalid Git source setting.") from xcp
        return GitMirrorSource(
            host=host,
            repository=require_string(value.get("repository"), label=f"{label}.repository"),
            tracking_mode=tracking_mode,
            ref=require_string(value.get("ref"), label=f"{label}.ref"),
        )
    return UploadArchiveSource(
        archive_sha256=require_string(value.get("archive_sha256"), label=f"{label}.archive_sha256"),
        original_filename=require_string(value.get("original_filename"), label=f"{label}.original_filename"),
    )


def format_byte_count(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value / (1024 * 1024):.1f} MiB"


def _parse_github_repository_link(parts: list[str]) -> GitRepositoryLink:
    if len(parts) == 2:
        return GitRepositoryLink(host=MirrorGitHost.GITHUB, repository=_repository_from_parts(parts))
    if len(parts) >= 4 and parts[2] == "tree":
        return GitRepositoryLink(
            host=MirrorGitHost.GITHUB,
            repository=_repository_from_parts(parts[:2]),
            tracking_mode=MirrorTrackingMode.BRANCH,
            ref="/".join(parts[3:]),
        )
    if len(parts) == 4 and parts[2] == "commit":
        return GitRepositoryLink(
            host=MirrorGitHost.GITHUB,
            repository=_repository_from_parts(parts[:2]),
            tracking_mode=MirrorTrackingMode.PINNED_COMMIT,
            ref=parts[3],
        )
    raise MirrorError("GitHub URLs must point at a project, a single branch, or a commit.")


def _parse_gitlab_repository_link(parts: list[str]) -> GitRepositoryLink:
    if "-" not in parts:
        return GitRepositoryLink(host=MirrorGitHost.GITLAB, repository=_repository_from_parts(parts))
    separator_index = parts.index("-")
    repository_parts = parts[:separator_index]
    link_parts = parts[separator_index + 1 :]
    if len(link_parts) < 2:
        raise MirrorError("GitLab URLs must point at a project, a single branch, or a commit.")
    if link_parts[0] == "tree":
        tracking_mode = MirrorTrackingMode.BRANCH
        ref = "/".join(link_parts[1:])
    elif link_parts[0] == "commit" and len(link_parts) == 2:
        tracking_mode = MirrorTrackingMode.PINNED_COMMIT
        ref = link_parts[1]
    else:
        raise MirrorError("GitLab URLs must point at a project, a single branch, or a commit.")
    return GitRepositoryLink(
        host=MirrorGitHost.GITLAB,
        repository=_repository_from_parts(repository_parts),
        tracking_mode=tracking_mode,
        ref=ref,
    )


def _repository_from_parts(parts: list[str]) -> str:
    if parts and parts[-1].endswith(".git"):
        parts = [*parts[:-1], parts[-1][:-4]]
    return normalise_repository_identifier("/".join(parts))


def require_string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MirrorError(f"{label} must be a non-empty string.")
    return value


def optional_string(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MirrorError(f"{label} must be a string when provided.")
    return value

"""Orchestrate validated, immutable update-mirror snapshots.

This module is intentionally limited to mirror lifecycle and scheduling. Domain
validation, Git-provider transport, snapshot storage, and the ComputerCraft
client asset live in their dedicated modules.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from _authority import read_json_object, write_json_object
from mirror_git import GitProviderClient
from mirror_models import (
    GitMirrorSource,
    GitReferenceOption,
    GitRepositoryInspection,
    MirrorArchiveLimits,
    MirrorAutoSyncOutcome,
    MirrorAutoSyncResult,
    MirrorError,
    MirrorGitHost,
    MirrorProject,
    MirrorSyncState,
    MirrorTrackingMode,
    UploadArchiveSource,
    format_byte_count,
    normalise_project_id,
    normalise_git_reference,
    normalise_utc_datetime,
    parse_public_git_repository_link,
    parse_public_git_repository_url,
    project_from_mapping,
    suggest_project_id,
    timestamp_as_utc_datetime,
    timestamp_from_datetime,
)
from mirror_snapshot import MirrorSnapshotStore

__all__ = ("MirrorService",)

_SCHEMA_VERSION: Final[int] = 1
_AUTO_SYNC_INTERVAL: Final[timedelta] = timedelta(days=1)
_AUTO_SYNC_INTERVAL_SECONDS: Final[int] = int(_AUTO_SYNC_INTERVAL.total_seconds())


@dataclass(frozen=True, slots=True)
class _SourceRefresh:
    archive_bytes: bytes | None
    revision: str


@dataclass(frozen=True, slots=True)
class _ScheduledCheckTimes:
    last_checked_at: str | None
    next_check_at: str | None


class MirrorService:
    """Own mirror metadata and coordinate source imports into immutable snapshots."""

    def __init__(
        self,
        storage_root: Path,
        *,
        archive_limits: MirrorArchiveLimits | None = None,
        git_client: GitProviderClient | None = None,
    ) -> None:
        self._storage_root = storage_root
        self._index_path = storage_root / "projects.json"
        self._uploads_root = storage_root / "uploads"
        self._archive_limits = archive_limits or MirrorArchiveLimits()
        self._git_client = git_client or GitProviderClient(archive_limits=self._archive_limits)
        self._snapshots = MirrorSnapshotStore(
            storage_root=storage_root / "snapshots",
            archive_limits=self._archive_limits,
        )
        self._lock = threading.RLock()
        self._refresh_locks: dict[str, threading.Lock] = {}
        self._projects = self._load_projects()
        self._recover_interrupted_syncs()

    def list_projects(self, *, actor_user_id: int, can_manage_all: bool) -> tuple[MirrorProject, ...]:
        with self._lock:
            projects = tuple(
                project
                for project in self._projects.values()
                if can_manage_all or project.owner_user_id == actor_user_id
            )
        return tuple(sorted(projects, key=lambda project: (project.display_name.casefold(), project.project_id)))

    def get_project(self, project_id: str) -> MirrorProject | None:
        with self._lock:
            return self._projects.get(normalise_project_id(project_id))

    def create_git_project(
        self,
        *,
        project_id: str,
        display_name: str,
        owner_user_id: int,
        repository_url: str,
        ref: str = "master",
        pinned_commit: bool = False,
        publish_root: str = "",
    ) -> MirrorProject:
        host, repository = parse_public_git_repository_url(repository_url)
        tracking_mode = MirrorTrackingMode.PINNED_COMMIT if pinned_commit else MirrorTrackingMode.BRANCH
        return self.create_git_project_from_source(
            project_id=project_id,
            display_name=display_name,
            owner_user_id=owner_user_id,
            source=GitMirrorSource(host=host, repository=repository, tracking_mode=tracking_mode, ref=ref),
            publish_root=publish_root,
        )

    def create_git_project_from_source(
        self,
        *,
        project_id: str,
        display_name: str,
        owner_user_id: int,
        source: GitMirrorSource,
        publish_root: str = "",
    ) -> MirrorProject:
        project = MirrorProject(
            project_id=project_id,
            display_name=display_name,
            owner_user_id=owner_user_id,
            source=source,
            publish_root=publish_root,
        )
        if project.is_auto_sync_eligible:
            project = dataclasses.replace(
                project,
                next_check_at=timestamp_from_datetime(
                    self._initial_auto_sync_time(project=project, now=_utc_now_datetime())
                ),
            )
        with self._lock:
            if project.project_id in self._projects:
                raise MirrorError(f"A mirror with ID {project.project_id!r} already exists.")
            self._projects[project.project_id] = project
            self._save_projects_locked()
        return project

    def inspect_git_repository_url(self, repository_url: str) -> GitRepositoryInspection:
        """Inspect a public provider URL and resolve its requested reference."""

        link = parse_public_git_repository_link(repository_url)
        metadata = self._git_client.fetch_repository_metadata(host=link.host, repository=link.repository)
        default_branch = self._provider_default_branch(metadata)
        if link.tracking_mode is MirrorTrackingMode.PINNED_COMMIT:
            if link.ref is None:
                raise MirrorError("Git commit link did not contain a commit SHA.")
            source = GitMirrorSource(
                host=link.host,
                repository=link.repository,
                tracking_mode=MirrorTrackingMode.PINNED_COMMIT,
                ref=self._git_client.resolve_revision(host=link.host, repository=link.repository, ref=link.ref),
            )
        else:
            branch = default_branch if link.ref is None else link.ref
            self._git_client.resolve_revision(host=link.host, repository=link.repository, ref=branch)
            source = GitMirrorSource(
                host=link.host,
                repository=link.repository,
                tracking_mode=MirrorTrackingMode.BRANCH,
                ref=branch,
            )
        return GitRepositoryInspection(
            source=source,
            display_name=self._provider_repository_name(metadata, fallback=link.repository.rsplit("/", maxsplit=1)[-1]),
            suggested_project_id=suggest_project_id(link.repository),
            default_branch=default_branch,
        )

    def list_git_reference_options(
        self,
        *,
        host: MirrorGitHost,
        repository: str,
        tracking_mode: MirrorTrackingMode,
    ) -> tuple[GitReferenceOption, ...]:
        """Return a bounded, provider-validated list of selectable Git references."""

        return self._git_client.list_reference_options(
            host=host,
            repository=repository,
            tracking_mode=tracking_mode,
        )

    def create_upload_project(
        self,
        *,
        project_id: str,
        display_name: str,
        owner_user_id: int,
        archive_path: Path,
        publish_root: str = "",
    ) -> MirrorProject:
        if archive_path.suffix.casefold() != ".zip":
            raise MirrorError("Mirror uploads must be ZIP archives.")
        normalised_id = normalise_project_id(project_id)
        with self._lock:
            if normalised_id in self._projects:
                raise MirrorError(f"A mirror with ID {normalised_id!r} already exists.")
            archive_sha256 = self._copy_upload_archive(project_id=normalised_id, archive_path=archive_path)
            project = MirrorProject(
                project_id=normalised_id,
                display_name=display_name,
                owner_user_id=owner_user_id,
                source=UploadArchiveSource(archive_sha256=archive_sha256, original_filename=archive_path.name),
                publish_root=publish_root,
            )
            try:
                self._projects[project.project_id] = project
                self._save_projects_locked()
            except Exception:
                self._upload_archive_path(project.project_id).unlink(missing_ok=True)
                raise
        return project

    def refresh_project(self, *, project_id: str, actor_user_id: int, can_manage_all: bool) -> MirrorProject:
        project_id = normalise_project_id(project_id)
        with self._refresh_lock(project_id):
            project = self._require_project_manager(
                project_id=project_id,
                actor_user_id=actor_user_id,
                can_manage_all=can_manage_all,
            )
            refreshed, _ = self._refresh_project_locked(project=project, automatic=False)
            return refreshed

    def sync_next_due_git_project(self, *, now: datetime | None = None) -> MirrorAutoSyncResult | None:
        """Check and publish at most one due branch-tracking Git mirror."""

        current_time = normalise_utc_datetime(now)
        self._assign_initial_auto_sync_times(now=current_time)
        project = self._next_due_auto_sync_project(now=current_time)
        if project is None:
            return None
        with self._refresh_lock(project.project_id):
            current_project = self.get_project(project.project_id)
            if current_project is None or not self._is_due_for_auto_sync(project=current_project, now=current_time):
                return None
            refreshed, outcome = self._refresh_project_locked(project=current_project, automatic=True)
        if outcome is None:
            raise RuntimeError("Scheduled Git mirror refresh did not report an outcome.")
        return MirrorAutoSyncResult(project_id=refreshed.project_id, outcome=outcome, project=refreshed)

    def pin_current_revision(self, *, project_id: str, actor_user_id: int, can_manage_all: bool) -> MirrorProject:
        project_id = normalise_project_id(project_id)
        with self._refresh_lock(project_id):
            project = self._require_project_manager(
                project_id=project_id,
                actor_user_id=actor_user_id,
                can_manage_all=can_manage_all,
            )
            if not isinstance(project.source, GitMirrorSource):
                raise MirrorError("Only Git mirrors can pin a revision.")
            revision = project.published_revision
            if revision is None or len(revision) != 40:
                raise MirrorError("Refresh this Git mirror successfully before pinning it.")
            updated_project = dataclasses.replace(
                project,
                source=dataclasses.replace(
                    project.source,
                    tracking_mode=MirrorTrackingMode.PINNED_COMMIT,
                    ref=revision,
                ),
                status_detail=f"Pinned to {revision[:12]}.",
            )
            self._replace_project(updated_project)
            return updated_project

    def track_master(self, *, project_id: str, actor_user_id: int, can_manage_all: bool) -> MirrorProject:
        project_id = normalise_project_id(project_id)
        with self._refresh_lock(project_id):
            project = self._require_project_manager(
                project_id=project_id,
                actor_user_id=actor_user_id,
                can_manage_all=can_manage_all,
            )
            if not isinstance(project.source, GitMirrorSource):
                raise MirrorError("Only Git mirrors can track a branch.")
            updated_project = dataclasses.replace(
                project,
                source=dataclasses.replace(
                    project.source,
                    tracking_mode=MirrorTrackingMode.BRANCH,
                    ref="master",
                ),
                status_detail="Tracking master. Refresh to publish its current commit.",
            )
            self._replace_project(updated_project)
            return updated_project

    def disable_project(self, *, project_id: str, actor_user_id: int, can_manage_all: bool) -> MirrorProject:
        project_id = normalise_project_id(project_id)
        with self._refresh_lock(project_id):
            project = self._require_project_manager(
                project_id=project_id,
                actor_user_id=actor_user_id,
                can_manage_all=can_manage_all,
            )
            updated_project = dataclasses.replace(
                project,
                sync_state=MirrorSyncState.DISABLED,
                status_detail="Disabled by its owner.",
            )
            self._replace_project(updated_project)
            return updated_project

    def manifest_path(self, project_id: str) -> Path | None:
        project = self.get_project(project_id)
        return None if project is None else self._snapshots.manifest_path(project)

    def file_path(self, *, project_id: str, relative_path: str, revision: str | None = None) -> Path | None:
        project = self.get_project(project_id)
        return None if project is None else self._snapshots.file_path(
            project=project,
            relative_path=relative_path,
            revision=revision,
        )

    def _refresh_project_locked(
        self,
        *,
        project: MirrorProject,
        automatic: bool,
    ) -> tuple[MirrorProject, MirrorAutoSyncOutcome | None]:
        if project.sync_state is MirrorSyncState.DISABLED:
            raise MirrorError(f"Mirror {project.project_id!r} is disabled.")
        self._replace_project(
            dataclasses.replace(
                project,
                sync_state=MirrorSyncState.PUBLISHING,
                status_detail="Checking tracked branch…" if automatic else "Fetching source…",
            )
        )
        try:
            source_refresh = self._refresh_source(project=project, automatic=automatic)
            if source_refresh.archive_bytes is None:
                source = project.source
                if not isinstance(source, GitMirrorSource):
                    raise AssertionError("Only Git mirrors can report an unchanged branch revision.")
                completed_at = _utc_now_datetime()
                scheduled_times = self._scheduled_check_times(project=project, completed_at=completed_at)
                unchanged_project = dataclasses.replace(
                    project,
                    sync_state=MirrorSyncState.PUBLISHED,
                    status_detail=f"Checked {source.ref}; revision {source_refresh.revision[:12]} is already published.",
                    last_checked_at=scheduled_times.last_checked_at,
                    next_check_at=scheduled_times.next_check_at,
                )
                self._replace_project(unchanged_project)
                return (unchanged_project, MirrorAutoSyncOutcome.UNCHANGED)
            publication = self._snapshots.publish(
                project=project,
                archive_bytes=source_refresh.archive_bytes,
                revision=source_refresh.revision,
                generated_at=_utc_now(),
            )
        except Exception as xcp:
            completed_at = _utc_now_datetime()
            scheduled_times = self._scheduled_check_times(project=project, completed_at=completed_at)
            failed_project = dataclasses.replace(
                project,
                sync_state=MirrorSyncState.FAILED,
                status_detail=self._failure_detail(automatic=automatic, exception=xcp),
                last_checked_at=scheduled_times.last_checked_at,
                next_check_at=scheduled_times.next_check_at,
            )
            self._replace_project(failed_project)
            if automatic:
                return (failed_project, MirrorAutoSyncOutcome.FAILED)
            if isinstance(xcp, MirrorError):
                raise
            raise MirrorError(f"Mirror refresh failed: {xcp}") from xcp
        completed_at = _utc_now_datetime()
        scheduled_times = self._scheduled_check_times(project=project, completed_at=completed_at)
        updated_project = dataclasses.replace(
            project,
            sync_state=MirrorSyncState.PUBLISHED,
            status_detail=("Automatically published" if automatic else "Published")
            + f" {publication.file_count} files ({format_byte_count(publication.extracted_bytes)}).",
            published_revision=source_refresh.revision,
            published_at=timestamp_from_datetime(completed_at),
            last_checked_at=scheduled_times.last_checked_at,
            next_check_at=scheduled_times.next_check_at,
        )
        self._replace_project(updated_project)
        return (updated_project, MirrorAutoSyncOutcome.PUBLISHED if automatic else None)

    def _refresh_source(self, *, project: MirrorProject, automatic: bool) -> _SourceRefresh:
        source = project.source
        if automatic:
            if not isinstance(source, GitMirrorSource) or source.tracking_mode is not MirrorTrackingMode.BRANCH:
                raise MirrorError("Only branch-tracking Git mirrors can be automatically synced.")
            revision = self._git_client.resolve_revision(host=source.host, repository=source.repository, ref=source.ref)
            if revision == project.published_revision:
                return _SourceRefresh(archive_bytes=None, revision=revision)
            return _SourceRefresh(
                archive_bytes=self._git_client.download_archive(source=source, revision=revision),
                revision=revision,
            )
        if isinstance(source, GitMirrorSource):
            revision = self._git_client.resolve_revision(host=source.host, repository=source.repository, ref=source.ref)
            return _SourceRefresh(
                archive_bytes=self._git_client.download_archive(source=source, revision=revision),
                revision=revision,
            )
        archive_path = self._upload_archive_path(project.project_id)
        try:
            if not archive_path.is_file():
                raise MirrorError("The uploaded archive is no longer available.")
            if archive_path.stat().st_size > self._archive_limits.archive_bytes:
                raise MirrorError(f"Uploaded archives must not exceed {format_byte_count(self._archive_limits.archive_bytes)}.")
            archive_bytes = archive_path.read_bytes()
        except OSError as xcp:
            raise MirrorError("The uploaded archive is no longer available.") from xcp
        if len(archive_bytes) > self._archive_limits.archive_bytes:
            raise MirrorError(f"Uploaded archives must not exceed {format_byte_count(self._archive_limits.archive_bytes)}.")
        digest = hashlib.sha256(archive_bytes).hexdigest()
        if digest != source.archive_sha256:
            raise MirrorError("The uploaded archive no longer matches its recorded SHA-256 digest.")
        return _SourceRefresh(archive_bytes=archive_bytes, revision=digest)

    def _assign_initial_auto_sync_times(self, *, now: datetime) -> None:
        with self._lock:
            updates = {
                project_id: dataclasses.replace(
                    project,
                    next_check_at=timestamp_from_datetime(self._initial_auto_sync_time(project=project, now=now)),
                )
                for project_id, project in self._projects.items()
                if project.is_auto_sync_eligible and project.next_check_at is None
            }
            if updates:
                self._projects.update(updates)
                self._save_projects_locked()

    def _next_due_auto_sync_project(self, *, now: datetime) -> MirrorProject | None:
        with self._lock:
            due_projects = tuple(
                project for project in self._projects.values() if self._is_due_for_auto_sync(project=project, now=now)
            )
        return min(
            due_projects,
            key=lambda project: (timestamp_as_utc_datetime(project.next_check_at or ""), project.project_id),
            default=None,
        )

    def _refresh_lock(self, project_id: str) -> threading.Lock:
        with self._lock:
            return self._refresh_locks.setdefault(project_id, threading.Lock())

    def _require_project_manager(
        self,
        *,
        project_id: str,
        actor_user_id: int,
        can_manage_all: bool,
    ) -> MirrorProject:
        project = self.get_project(project_id)
        if project is None:
            raise MirrorError(f"Unknown mirror project: {project_id}")
        if not can_manage_all and project.owner_user_id != actor_user_id:
            raise PermissionError("You do not own this mirror project.")
        return project

    def _replace_project(self, project: MirrorProject) -> None:
        with self._lock:
            self._projects[project.project_id] = project
            self._save_projects_locked()

    def _load_projects(self) -> dict[str, MirrorProject]:
        if not self._index_path.exists():
            return {}
        payload = read_json_object(self._index_path)
        schema = payload.get("schema")
        if schema != _SCHEMA_VERSION:
            raise MirrorError(f"Unsupported mirror project schema: {schema!r}")
        raw_projects = payload.get("projects")
        if not isinstance(raw_projects, list):
            raise MirrorError("Mirror project index must contain a projects array.")
        projects: dict[str, MirrorProject] = {}
        for index, raw_project in enumerate(raw_projects):
            project = project_from_mapping(raw_project, label=f"projects[{index}]")
            if project.project_id in projects:
                raise MirrorError(f"Mirror project index contains duplicate ID: {project.project_id}")
            projects[project.project_id] = project
        return projects

    def _recover_interrupted_syncs(self) -> None:
        with self._lock:
            recovered_projects = {
                project_id: dataclasses.replace(
                    project,
                    sync_state=MirrorSyncState.FAILED,
                    status_detail="Previous mirror sync was interrupted; retry the mirror to publish a new snapshot.",
                )
                for project_id, project in self._projects.items()
                if project.sync_state is MirrorSyncState.PUBLISHING
            }
            if recovered_projects:
                self._projects.update(recovered_projects)
                self._save_projects_locked()

    def _save_projects_locked(self) -> None:
        write_json_object(
            self._index_path,
            {
                "schema": _SCHEMA_VERSION,
                "projects": [
                    project.to_mapping() for project in sorted(self._projects.values(), key=lambda project: project.project_id)
                ],
            },
        )

    def _copy_upload_archive(self, *, project_id: str, archive_path: Path) -> str:
        try:
            source_size = archive_path.stat().st_size
        except OSError as xcp:
            raise MirrorError(f"Unable to read uploaded archive: {archive_path}") from xcp
        if source_size > self._archive_limits.archive_bytes:
            raise MirrorError(f"Uploaded archives must not exceed {format_byte_count(self._archive_limits.archive_bytes)}.")
        self._uploads_root.mkdir(parents=True, exist_ok=True)
        destination = self._upload_archive_path(project_id)
        temporary_path: Path | None = None
        digest = hashlib.sha256()
        total_bytes = 0
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self._uploads_root,
                prefix=f".{project_id}.",
                suffix=".tmp",
                delete=False,
            ) as target, archive_path.open("rb") as source:
                temporary_path = Path(target.name)
                while chunk := source.read(self._archive_limits.copy_chunk_bytes):
                    total_bytes += len(chunk)
                    if total_bytes > self._archive_limits.archive_bytes:
                        raise MirrorError(
                            f"Uploaded archives must not exceed {format_byte_count(self._archive_limits.archive_bytes)}."
                        )
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            temporary_path.replace(destination)
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise
        return digest.hexdigest()

    def _upload_archive_path(self, project_id: str) -> Path:
        return self._uploads_root / f"{normalise_project_id(project_id)}.zip"

    @staticmethod
    def _initial_auto_sync_time(*, project: MirrorProject, now: datetime) -> datetime:
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        offset_seconds = int.from_bytes(
            hashlib.sha256(project.project_id.encode("utf-8")).digest()[:8],
            byteorder="big",
        ) % _AUTO_SYNC_INTERVAL_SECONDS
        candidate = day_start + timedelta(seconds=offset_seconds)
        return candidate + _AUTO_SYNC_INTERVAL if candidate <= now else candidate

    @staticmethod
    def _is_due_for_auto_sync(*, project: MirrorProject | None, now: datetime) -> bool:
        return (
            project is not None
            and project.is_auto_sync_eligible
            and project.sync_state is not MirrorSyncState.PUBLISHING
            and project.next_check_at is not None
            and timestamp_as_utc_datetime(project.next_check_at) <= now
        )

    def _scheduled_check_times(self, *, project: MirrorProject, completed_at: datetime) -> _ScheduledCheckTimes:
        if not project.is_auto_sync_eligible:
            return _ScheduledCheckTimes(last_checked_at=None, next_check_at=None)
        next_check_at = project.next_check_at
        if next_check_at is None or timestamp_as_utc_datetime(next_check_at) <= completed_at:
            next_check_at = timestamp_from_datetime(self._initial_auto_sync_time(project=project, now=completed_at))
        return _ScheduledCheckTimes(
            last_checked_at=timestamp_from_datetime(completed_at),
            next_check_at=next_check_at,
        )

    @staticmethod
    def _provider_repository_name(metadata: dict[str, object], *, fallback: str) -> str:
        name = metadata.get("name")
        return name.strip() if isinstance(name, str) and name.strip() else fallback

    @staticmethod
    def _provider_default_branch(metadata: dict[str, object]) -> str:
        default_branch = metadata.get("default_branch")
        if not isinstance(default_branch, str) or not default_branch.strip():
            raise MirrorError("Git repository does not have a default branch to mirror.")
        return normalise_git_reference(default_branch, tracking_mode=MirrorTrackingMode.BRANCH)

    @staticmethod
    def _failure_detail(*, automatic: bool, exception: Exception) -> str:
        prefix = "Automatic mirror check failed" if automatic else "Mirror refresh failed"
        detail = f"{prefix}: {exception}".strip()
        return detail[:500] or prefix


def _utc_now_datetime() -> datetime:
    """Return the injectable clock used by lifecycle and scheduler operations."""

    return datetime.now(UTC)


def _utc_now() -> str:
    return timestamp_from_datetime(_utc_now_datetime())

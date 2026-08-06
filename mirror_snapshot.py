"""Safe ZIP extraction and immutable snapshot storage for update mirrors."""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import os
import shutil
import stat
import tempfile
import urllib.parse
import zipfile
from pathlib import Path, PurePosixPath
from typing import Final

from mirror_models import (
    GitMirrorSource,
    MirrorArchiveLimits,
    MirrorError,
    MirrorFileRecord,
    MirrorProject,
    MirrorRevisionUnavailable,
    format_byte_count,
    normalise_project_id,
    normalise_published_revision,
)

_MANIFEST_SCHEMA_VERSION: Final[int] = 1


@dataclasses.dataclass(frozen=True, slots=True)
class SnapshotPublication:
    file_count: int
    extracted_bytes: int


class MirrorSnapshotStore:
    """Publish and retrieve validated, immutable project snapshots."""

    def __init__(self, *, storage_root: Path, archive_limits: MirrorArchiveLimits) -> None:
        self._storage_root = storage_root
        self._archive_limits = archive_limits

    def publish(
        self,
        *,
        project: MirrorProject,
        archive_bytes: bytes,
        revision: str,
        generated_at: str,
    ) -> SnapshotPublication:
        self._storage_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"{project.project_id}-", dir=self._storage_root) as temporary_name:
            temporary_root = Path(temporary_name)
            files_root = temporary_root / "files"
            files_root.mkdir()
            file_count, extracted_bytes = self._extract_archive(
                archive_bytes=archive_bytes,
                project=project,
                output_root=files_root,
            )
            records = self._scan_files(files_root)
            if len(records) != file_count:
                raise MirrorError("Mirror archive extraction produced an unexpected file count.")
            manifest = {
                "schema": _MANIFEST_SCHEMA_VERSION,
                "project": project.project_id,
                "revision": revision,
                "generated_at": generated_at,
                "source": project.source.to_mapping(),
                "publish_root": project.publish_root,
                "files": [record.to_mapping() for record in records],
            }
            (temporary_root / "manifest.json").write_text(
                json.dumps(manifest, sort_keys=True, indent=4) + "\n",
                encoding="utf-8",
            )
            self._publish_snapshot(project_id=project.project_id, revision=revision, temporary_root=temporary_root)
        return SnapshotPublication(file_count=file_count, extracted_bytes=extracted_bytes)

    def manifest_path(self, project: MirrorProject) -> Path | None:
        if not project.is_snapshot_available or project.published_revision is None:
            return None
        snapshot_root = self._snapshot_path(project=project, revision=project.published_revision)
        if snapshot_root.is_symlink():
            return None
        path = snapshot_root / "manifest.json"
        return path if path.is_file() and not path.is_symlink() else None

    def file_path(self, *, project: MirrorProject, relative_path: str, revision: str | None) -> Path | None:
        if not project.is_snapshot_available or project.published_revision is None:
            return None
        try:
            safe_path = self._safe_relative_path(relative_path)
        except MirrorError:
            return None
        snapshot_revision = project.published_revision if revision is None else normalise_published_revision(revision)
        if snapshot_revision is None:
            raise MirrorError("Mirror file revisions must not be empty.")
        snapshot_root = self._snapshot_path(project=project, revision=snapshot_revision)
        if not snapshot_root.is_dir() or snapshot_root.is_symlink():
            raise MirrorRevisionUnavailable(
                "The requested mirror snapshot is no longer available. Fetch a new manifest."
            )
        path = snapshot_root / "files" / safe_path
        return path if path.is_file() and not path.is_symlink() else None

    def _extract_archive(self, *, archive_bytes: bytes, project: MirrorProject, output_root: Path) -> tuple[int, int]:
        try:
            archive = zipfile.ZipFile(io.BytesIO(archive_bytes))
        except zipfile.BadZipFile as xcp:
            raise MirrorError("Mirror source is not a valid ZIP archive.") from xcp
        with archive:
            archive_infos = archive.infolist()
            if len(archive_infos) > self._archive_limits.archive_member_count:
                raise MirrorError(
                    f"Mirror source exceeds the {self._archive_limits.archive_member_count} archive-member limit."
                )
            archive_members = tuple((info, self._safe_zip_member_path(info)) for info in archive_infos)
            entries = tuple((info, path) for info, path in archive_members if not info.is_dir())
            if not entries:
                raise MirrorError("Mirror source archive does not contain any files.")
            if len(entries) > self._archive_limits.file_count:
                raise MirrorError(f"Mirror source exceeds the {self._archive_limits.file_count} file limit.")
            if sum(info.file_size for info, _ in entries) > self._archive_limits.extracted_bytes:
                raise MirrorError(
                    "Mirror source exceeds the "
                    f"{format_byte_count(self._archive_limits.extracted_bytes)} extracted-size limit."
                )
            source_root = self._git_archive_root(entries) if isinstance(project.source, GitMirrorSource) else None
            publish_root = PurePosixPath(project.publish_root) if project.publish_root else None
            output_paths: set[str] = set()
            output_path_keys: set[str] = set()
            file_count = 0
            extracted_bytes = 0
            for info, archive_path in entries:
                source_path = self._strip_git_archive_root(archive_path, root=source_root)
                if source_path is None:
                    continue
                target_relative_path = self._path_under_publish_root(source_path, publish_root=publish_root)
                if target_relative_path is None:
                    continue
                target_relative_text = target_relative_path.as_posix()
                if target_relative_text in output_paths or target_relative_text.casefold() in output_path_keys:
                    raise MirrorError(f"Mirror source contains duplicate output path: {target_relative_text}")
                if info.file_size > self._archive_limits.file_bytes:
                    raise MirrorError(
                        "Mirror file exceeds the "
                        f"{format_byte_count(self._archive_limits.file_bytes)} size limit: {target_relative_text}"
                    )
                output_paths.add(target_relative_text)
                output_path_keys.add(target_relative_text.casefold())
                target_path = output_root / target_relative_text
                target_path.parent.mkdir(parents=True, exist_ok=True)
                written_bytes = 0
                with archive.open(info, "r") as source, target_path.open("xb") as target:
                    while chunk := source.read(self._archive_limits.copy_chunk_bytes):
                        written_bytes += len(chunk)
                        extracted_bytes += len(chunk)
                        if written_bytes > self._archive_limits.file_bytes:
                            raise MirrorError(
                                "Mirror file exceeds the "
                                f"{format_byte_count(self._archive_limits.file_bytes)} size limit."
                            )
                        if extracted_bytes > self._archive_limits.extracted_bytes:
                            raise MirrorError(
                                "Mirror source exceeds the "
                                f"{format_byte_count(self._archive_limits.extracted_bytes)} extracted-size limit."
                            )
                        target.write(chunk)
                if written_bytes != info.file_size:
                    raise MirrorError(f"Mirror archive member size changed while extracting: {target_relative_text}")
                file_count += 1
            if file_count == 0:
                raise MirrorError(f"Publish root does not contain files: {project.publish_root or '/'}")
            return (file_count, extracted_bytes)

    def _publish_snapshot(self, *, project_id: str, revision: str, temporary_root: Path) -> None:
        normalised_revision = normalise_published_revision(revision)
        if normalised_revision is None:
            raise MirrorError("Published snapshots must have a revision.")
        revisions_root = self._snapshot_revisions_root(project_id)
        revisions_root.mkdir(parents=True, exist_ok=True)
        destination = revisions_root / normalised_revision
        next_path = revisions_root / f".{normalised_revision}.next"
        self._remove_tree_if_present(next_path)
        os.replace(temporary_root, next_path)
        try:
            if not destination.exists():
                os.replace(next_path, destination)
        finally:
            self._remove_tree_if_present(next_path)
        self._prune_snapshot_revisions(project_id=project_id, current_revision=normalised_revision)

    def _snapshot_path(self, *, project: MirrorProject, revision: str) -> Path:
        normalised_revision = normalise_published_revision(revision)
        if normalised_revision is None:
            raise MirrorError("Published snapshots must have a revision.")
        revision_path = self._snapshot_revisions_root(project.project_id) / normalised_revision
        if revision_path.is_dir() and not revision_path.is_symlink():
            return revision_path
        if project.published_revision == normalised_revision:
            legacy_root = self._project_root(project.project_id)
            if legacy_root.is_dir() and not legacy_root.is_symlink():
                return legacy_root
        return revision_path

    def _prune_snapshot_revisions(self, *, project_id: str, current_revision: str) -> None:
        revisions_root = self._snapshot_revisions_root(project_id)
        revisions = tuple(
            path
            for path in revisions_root.iterdir()
            if path.is_dir() and not path.is_symlink() and self._is_valid_revision_name(path.name)
        )
        current_path = revisions_root / current_revision
        retained_paths = frozenset(
            (current_path,)
            + tuple(
                path
                for path in sorted(revisions, key=lambda path: path.stat().st_mtime_ns, reverse=True)
                if path != current_path
            )[: self._archive_limits.retained_snapshot_revisions - 1]
        )
        for path in revisions:
            if path not in retained_paths:
                shutil.rmtree(path)

    def _project_root(self, project_id: str) -> Path:
        return self._storage_root / normalise_project_id(project_id)

    def _snapshot_revisions_root(self, project_id: str) -> Path:
        return self._project_root(project_id) / "revisions"

    @staticmethod
    def _safe_zip_member_path(info: zipfile.ZipInfo) -> PurePosixPath:
        if info.flag_bits & 0x1:
            raise MirrorError(f"Refusing encrypted archive member: {info.filename}")
        if "\\" in info.filename or "\x00" in info.filename:
            raise MirrorError(f"Refusing unsafe archive member: {info.filename}")
        raw_path = info.filename.rstrip("/")
        if not raw_path or raw_path.startswith("/"):
            raise MirrorError(f"Refusing unsafe archive member: {info.filename}")
        parts = raw_path.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise MirrorError(f"Refusing unsafe archive member: {info.filename}")
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise MirrorError(f"Refusing symlinked archive member: {info.filename}")
        file_type = stat.S_IFMT(mode)
        expected_type = stat.S_IFDIR if info.is_dir() else stat.S_IFREG
        if file_type not in {0, expected_type}:
            raise MirrorError(f"Refusing non-regular archive member: {info.filename}")
        return PurePosixPath(*parts)

    @staticmethod
    def _git_archive_root(entries: tuple[tuple[zipfile.ZipInfo, PurePosixPath], ...]) -> str:
        roots = {path.parts[0] for _, path in entries if path.parts}
        if len(roots) != 1:
            raise MirrorError("Git provider archive must contain exactly one top-level source directory.")
        return next(iter(roots))

    @staticmethod
    def _strip_git_archive_root(path: PurePosixPath, *, root: str | None) -> PurePosixPath | None:
        if root is None:
            return path
        if not path.parts or path.parts[0] != root:
            raise MirrorError(f"Git provider archive contained an unexpected path: {path.as_posix()}")
        return PurePosixPath(*path.parts[1:]) if len(path.parts) > 1 else None

    @staticmethod
    def _path_under_publish_root(
        path: PurePosixPath,
        *,
        publish_root: PurePosixPath | None,
    ) -> PurePosixPath | None:
        if publish_root is None:
            return path
        prefix_length = len(publish_root.parts)
        if path.parts[:prefix_length] != publish_root.parts or len(path.parts) == prefix_length:
            return None
        return PurePosixPath(*path.parts[prefix_length:])

    @staticmethod
    def _safe_relative_path(value: str) -> str:
        decoded = urllib.parse.unquote(value)
        if "\\" in decoded or "\x00" in decoded:
            raise MirrorError("Mirror file path is invalid.")
        path = PurePosixPath(decoded)
        if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
            raise MirrorError("Mirror file path is invalid.")
        return path.as_posix()

    def _scan_files(self, root: Path) -> tuple[MirrorFileRecord, ...]:
        records: list[MirrorFileRecord] = []
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.is_symlink():
                records.append(
                    MirrorFileRecord(
                        path=path.relative_to(root).as_posix(),
                        size=path.stat().st_size,
                        sha256=self._file_sha256(path),
                    )
                )
        return tuple(records)

    def _file_sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(self._archive_limits.copy_chunk_bytes):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _remove_tree_if_present(path: Path) -> None:
        if not path.exists():
            return
        if not path.is_dir():
            raise MirrorError(f"Mirror snapshot path is not a directory: {path}")
        shutil.rmtree(path)

    @staticmethod
    def _is_valid_revision_name(value: str) -> bool:
        try:
            return normalise_published_revision(value) is not None
        except MirrorError:
            return False

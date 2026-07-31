"""Validated metadata describing a deployed Yukibot revision."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Final

DEPLOYMENT_METADATA_RELATIVE_PATH: Final[Path] = Path(".yukibot/deployment.json")
DEPLOYMENT_METADATA_SCHEMA_VERSION: Final[int] = 1
_COMMIT_SHA_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{7,40}")
_TARGET_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9-]*")


def parse_deployment_revision(raw: str) -> str:
    """Normalise and validate a Git commit SHA used as deployment identity."""
    value: str = raw.strip().lower()
    if not _COMMIT_SHA_PATTERN.fullmatch(value):
        raise ValueError("Deployment revisions must be a 7-40 character hexadecimal Git commit SHA.")
    return value


def parse_optional_deployment_revision(raw: str | None) -> str | None:
    if raw is None:
        return None
    return parse_deployment_revision(raw)


@dataclass(frozen=True, slots=True)
class DeploymentMetadata:
    revision: str
    deployed_at: datetime
    target_name: str
    version: str | None = None
    source_paths: tuple[PurePosixPath, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "revision", parse_deployment_revision(self.revision))

        if self.deployed_at.tzinfo is None:
            raise ValueError("Deployment timestamps must include a timezone.")
        object.__setattr__(self, "deployed_at", self.deployed_at.astimezone(timezone.utc))

        target_name: str = self.target_name.strip().lower()
        if not _TARGET_NAME_PATTERN.fullmatch(target_name):
            raise ValueError("Deployment target names must use lowercase letters, numbers, and hyphens.")
        object.__setattr__(self, "target_name", target_name)

        if self.version is not None:
            version: str = self.version.strip()
            if not version:
                raise ValueError("Deployment versions must not be blank when provided.")
            object.__setattr__(self, "version", version)

        source_paths: list[PurePosixPath] = []
        for source_path in self.source_paths:
            path: PurePosixPath = PurePosixPath(source_path)
            if path.is_absolute() or path == PurePosixPath(".") or ".." in path.parts:
                raise ValueError("Deployment source paths must be safe project-relative paths.")
            source_paths.append(path)
        if len(set(source_paths)) != len(source_paths):
            raise ValueError("Deployment source paths must not contain duplicates.")
        object.__setattr__(self, "source_paths", tuple(sorted(source_paths, key=lambda path: path.as_posix())))

    def to_json(self) -> str:
        return json.dumps(
            {
                "deployed_at": self.deployed_at.isoformat().replace("+00:00", "Z"),
                "revision": self.revision,
                "schema_version": DEPLOYMENT_METADATA_SCHEMA_VERSION,
                "source_paths": [path.as_posix() for path in self.source_paths],
                "target_name": self.target_name,
                "version": self.version,
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str) -> "DeploymentMetadata":
        try:
            payload: object = json.loads(raw)
        except json.JSONDecodeError as xcp:
            raise ValueError("Deployment metadata must contain valid JSON.") from xcp
        if not isinstance(payload, dict):
            raise ValueError("Deployment metadata must be a JSON object.")
        if payload.get("schema_version") != DEPLOYMENT_METADATA_SCHEMA_VERSION:
            raise ValueError(f"Unsupported deployment metadata schema: {payload.get('schema_version')!r}.")

        revision: object = payload.get("revision")
        deployed_at: object = payload.get("deployed_at")
        target_name: object = payload.get("target_name")
        version: object = payload.get("version")
        source_paths: object = payload.get("source_paths", [])
        if not isinstance(revision, str):
            raise ValueError("Deployment metadata revision must be a string.")
        if not isinstance(deployed_at, str):
            raise ValueError("Deployment metadata deployed_at must be a string.")
        if not isinstance(target_name, str):
            raise ValueError("Deployment metadata target_name must be a string.")
        if version is not None and not isinstance(version, str):
            raise ValueError("Deployment metadata version must be a string or null.")
        if not isinstance(source_paths, list) or not all(isinstance(path, str) for path in source_paths):
            raise ValueError("Deployment metadata source_paths must be a list of strings.")
        try:
            parsed_deployed_at: datetime = datetime.fromisoformat(deployed_at.replace("Z", "+00:00"))
        except ValueError as xcp:
            raise ValueError("Deployment metadata deployed_at must be an ISO-8601 timestamp.") from xcp
        return cls(
            revision=revision,
            deployed_at=parsed_deployed_at,
            target_name=target_name,
            version=version,
            source_paths=tuple(PurePosixPath(path) for path in source_paths),
        )


def load_deployment_metadata(*, project_root: Path) -> DeploymentMetadata | None:
    metadata_path: Path = project_root / DEPLOYMENT_METADATA_RELATIVE_PATH
    if not metadata_path.exists():
        return None
    return DeploymentMetadata.from_json(metadata_path.read_text(encoding="utf-8"))

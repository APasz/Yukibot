from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from deployment_metadata import DEPLOYMENT_METADATA_RELATIVE_PATH, DeploymentMetadata, load_deployment_metadata


class DeploymentMetadataTests(unittest.TestCase):
    def test_metadata_normalises_revision_target_and_timestamp(self) -> None:
        metadata = DeploymentMetadata(
            revision=" ABCDEF123456 ",
            deployed_at=datetime(2026, 7, 31, 4, 30, tzinfo=timezone.utc),
            target_name=" Portal ",
            version=" v2026.07.31.1 ",
        )

        self.assertEqual(metadata.revision, "abcdef123456")
        self.assertEqual(metadata.target_name, "portal")
        self.assertEqual(metadata.version, "v2026.07.31.1")
        self.assertEqual(metadata.deployed_at, datetime(2026, 7, 31, 4, 30, tzinfo=timezone.utc))

    def test_metadata_round_trip_preserves_values(self) -> None:
        metadata = DeploymentMetadata(
            revision="abcdef123456",
            deployed_at=datetime(2026, 7, 31, 4, 30, tzinfo=timezone.utc),
            target_name="portal",
        )

        self.assertEqual(DeploymentMetadata.from_json(metadata.to_json()), metadata)

    def test_metadata_rejects_naive_timestamps(self) -> None:
        with self.assertRaisesRegex(ValueError, "include a timezone"):
            DeploymentMetadata(
                revision="abcdef123456",
                deployed_at=datetime(2026, 7, 31, 4, 30),
                target_name="portal",
            )

    def test_load_metadata_returns_none_when_not_deployed(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            metadata = load_deployment_metadata(project_root=Path(temporary_directory))

        self.assertIsNone(metadata)

    def test_load_metadata_reads_deployment_file(self) -> None:
        expected = DeploymentMetadata(
            revision="abcdef123456",
            deployed_at=datetime(2026, 7, 31, 4, 30, tzinfo=timezone.utc),
            target_name="portal",
            version="v2026.07.31.1",
        )
        with TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            metadata_path = project_root / DEPLOYMENT_METADATA_RELATIVE_PATH
            metadata_path.parent.mkdir()
            metadata_path.write_text(expected.to_json(), encoding="utf-8")

            metadata = load_deployment_metadata(project_root=project_root)

        self.assertEqual(metadata, expected)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import io
import json
import unittest
import zipfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from computercraft_mirror import (
    COMPUTERCRAFT_MIRROR_STATE_ROOT,
    COMPUTERCRAFT_MIRROR_STARTUP_DISPATCHER_PATH,
    COMPUTERCRAFT_MIRROR_INSTALLER,
)
from mirror_models import (
    MirrorAutoSyncOutcome,
    GitMirrorSource,
    MirrorError,
    MirrorGitHost,
    MirrorProject,
    MirrorRevisionUnavailable,
    MirrorSyncState,
    MirrorTrackingMode,
    parse_public_git_repository_link,
    parse_public_git_repository_url,
)
from mirror_service import MirrorService
from web_dash.mirrors import ModWebMirrorsMixin


class MirrorServiceCheck(unittest.TestCase):
    def test_uploaded_zip_publishes_selected_root_and_persists(self) -> None:
        with TemporaryDirectory() as temporary_name:
            temporary_root = Path(temporary_name)
            archive_path = temporary_root / "release.zip"
            self._write_zip(
                archive_path,
                {
                    "release/startup.lua": b"print('hello')\n",
                    "release/lib/util.lua": b"return {}\n",
                    "README.md": b"not published\n",
                },
            )
            service = MirrorService(temporary_root / "mirrors")
            project = service.create_upload_project(
                project_id="hello-world",
                display_name="Hello world",
                owner_user_id=42,
                archive_path=archive_path,
                publish_root="release",
            )

            refreshed = service.refresh_project(
                project_id=project.project_id,
                actor_user_id=42,
                can_manage_all=False,
            )

            self.assertEqual(refreshed.sync_state, MirrorSyncState.PUBLISHED)
            self.assertIsNotNone(refreshed.published_revision)
            manifest_path = service.manifest_path("hello-world")
            if manifest_path is None:
                self.fail("Published mirror did not write a manifest.")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual([record["path"] for record in manifest["files"]], ["lib/util.lua", "startup.lua"])
            startup_path = service.file_path(project_id="hello-world", relative_path="startup.lua")
            if startup_path is None:
                self.fail("Published mirror did not expose startup.lua.")
            self.assertEqual(
                startup_path.read_bytes(),
                b"print('hello')\n",
            )
            self.assertIsNone(service.file_path(project_id="hello-world", relative_path="README.md"))
            self.assertIsNone(service.file_path(project_id="hello-world", relative_path="../projects.json"))

            reloaded = MirrorService(temporary_root / "mirrors")
            self.assertEqual(reloaded.list_projects(actor_user_id=42, can_manage_all=False), (refreshed,))

    def test_git_mirror_resolves_master_to_an_immutable_commit(self) -> None:
        archive_bytes = self._zip_bytes(
            {
                "example-commit/startup.lua": b"print('hello')\n",
                "example-commit/docs/readme.md": b"docs\n",
            }
        )
        with TemporaryDirectory() as temporary_name:
            service = MirrorService(Path(temporary_name) / "mirrors")
            project = service.create_git_project(
                project_id="example",
                display_name="Example",
                owner_user_id=42,
                repository_url="https://github.com/example/project.git",
                ref="master",
                publish_root="",
            )
            with (
                patch.object(service._git_client, "fetch_json", return_value={"sha": "a" * 40}) as fetch_json,
                patch.object(service._git_client, "download_bytes", return_value=archive_bytes) as download_bytes,
            ):
                refreshed = service.refresh_project(
                    project_id=project.project_id,
                    actor_user_id=42,
                    can_manage_all=False,
                )

            self.assertEqual(refreshed.published_revision, "a" * 40)
            self.assertEqual(fetch_json.call_args.args[0], "https://api.github.com/repos/example/project/commits/master")
            self.assertEqual(download_bytes.call_args.args[0], f"https://codeload.github.com/example/project/zip/{'a' * 40}")
            pinned = service.pin_current_revision(project_id="example", actor_user_id=42, can_manage_all=False)
            source = pinned.source
            if not isinstance(source, GitMirrorSource):
                self.fail("Pinned Git mirror unexpectedly changed source type.")
            self.assertEqual(source.ref, "a" * 40)
            self.assertEqual(source.tracking_mode.value, "pinned_commit")

    def test_revision_qualified_file_paths_retain_recent_snapshots(self) -> None:
        first_archive = self._zip_bytes({"project/startup.lua": b"print('first')\n"})
        second_archive = self._zip_bytes({"project/startup.lua": b"print('second')\n"})
        first_revision = "a" * 40
        second_revision = "b" * 40
        with TemporaryDirectory() as temporary_name:
            service = MirrorService(Path(temporary_name) / "mirrors")
            project = service.create_git_project(
                project_id="project",
                display_name="Project",
                owner_user_id=42,
                repository_url="https://github.com/example/project",
            )
            with (
                patch.object(service._git_client, "fetch_json", side_effect=[{"sha": first_revision}, {"sha": second_revision}]),
                patch.object(service._git_client, "download_bytes", side_effect=[first_archive, second_archive]),
            ):
                service.refresh_project(project_id=project.project_id, actor_user_id=42, can_manage_all=False)
                service.refresh_project(project_id=project.project_id, actor_user_id=42, can_manage_all=False)

            old_file = service.file_path(
                project_id=project.project_id,
                relative_path="startup.lua",
                revision=first_revision,
            )
            current_file = service.file_path(
                project_id=project.project_id,
                relative_path="startup.lua",
                revision=second_revision,
            )
            if old_file is None or current_file is None:
                self.fail("Published revision did not expose its startup file.")
            self.assertEqual(old_file.read_bytes(), b"print('first')\n")
            self.assertEqual(current_file.read_bytes(), b"print('second')\n")
            with self.assertRaises(MirrorRevisionUnavailable):
                service.file_path(
                    project_id=project.project_id,
                    relative_path="startup.lua",
                    revision="c" * 40,
                )

    def test_refresh_failure_keeps_last_published_snapshot_available(self) -> None:
        archive = self._zip_bytes({"project/startup.lua": b"print('published')\n"})
        revision = "a" * 40
        with TemporaryDirectory() as temporary_name:
            service = MirrorService(Path(temporary_name) / "mirrors")
            project = service.create_git_project(
                project_id="project",
                display_name="Project",
                owner_user_id=42,
                repository_url="https://github.com/example/project",
            )
            with (
                patch.object(service._git_client, "fetch_json", return_value={"sha": revision}),
                patch.object(service._git_client, "download_bytes", return_value=archive),
            ):
                service.refresh_project(project_id=project.project_id, actor_user_id=42, can_manage_all=False)

            with (
                patch.object(service._git_client, "fetch_json", side_effect=MirrorError("Git provider is unavailable.")),
                self.assertRaisesRegex(MirrorError, "Git provider is unavailable"),
            ):
                service.refresh_project(project_id=project.project_id, actor_user_id=42, can_manage_all=False)

            failed_project = service.get_project(project.project_id)
            if failed_project is None:
                self.fail("Failed mirror project disappeared.")
            self.assertEqual(failed_project.sync_state, MirrorSyncState.FAILED)
            self.assertTrue(failed_project.is_snapshot_available)
            self.assertIsNotNone(service.manifest_path(project.project_id))
            self.assertIsNotNone(
                service.file_path(
                    project_id=project.project_id,
                    relative_path="startup.lua",
                    revision=revision,
                )
            )

    def test_automatic_branch_check_is_persisted_and_skips_unchanged_archive(self) -> None:
        archive = self._zip_bytes({"project/startup.lua": b"print('published')\n"})
        revision = "a" * 40
        now = datetime(2026, 8, 6, 8, 0, tzinfo=UTC)
        with TemporaryDirectory() as temporary_name:
            storage_root = Path(temporary_name) / "mirrors"
            service = MirrorService(storage_root)
            with patch("mirror_service._utc_now_datetime", return_value=now):
                project = service.create_git_project(
                    project_id="project",
                    display_name="Project",
                    owner_user_id=42,
                    repository_url="https://github.com/example/project",
                )

            self.assertIsNone(service.sync_next_due_git_project(now=now))
            scheduled = service.get_project(project.project_id)
            if scheduled is None or scheduled.next_check_at is None:
                self.fail("Branch mirror was not assigned an initial scheduled check.")
            self.assertGreater(
                datetime.fromisoformat(scheduled.next_check_at),
                now,
            )
            self.assertLessEqual(
                datetime.fromisoformat(scheduled.next_check_at),
                now + timedelta(days=1),
            )
            self.assertEqual(
                MirrorService(storage_root).get_project(project.project_id),
                scheduled,
            )

            due = replace(scheduled, next_check_at=(now - timedelta(seconds=1)).isoformat())
            service._replace_project(due)
            with (
                patch("mirror_service._utc_now_datetime", return_value=now),
                patch.object(service._git_client, "fetch_json", return_value={"sha": revision}),
                patch.object(service._git_client, "download_bytes", return_value=archive) as download_bytes,
            ):
                published_result = service.sync_next_due_git_project(now=now)

            if published_result is None:
                self.fail("Due branch mirror was not checked.")
            self.assertEqual(published_result.outcome, MirrorAutoSyncOutcome.PUBLISHED)
            self.assertEqual(published_result.project.published_revision, revision)
            self.assertEqual(published_result.project.last_checked_at, now.isoformat())
            self.assertEqual(
                published_result.project.next_check_at,
                service._initial_auto_sync_time(project=due, now=now).isoformat(),
            )
            download_bytes.assert_called_once()

            service._replace_project(
                replace(published_result.project, next_check_at=(now - timedelta(seconds=1)).isoformat())
            )
            with (
                patch("mirror_service._utc_now_datetime", return_value=now + timedelta(days=1)),
                patch.object(service._git_client, "fetch_json", return_value={"sha": revision}),
                patch.object(service._git_client, "download_bytes") as download_bytes,
            ):
                unchanged_result = service.sync_next_due_git_project(now=now)

            if unchanged_result is None:
                self.fail("Due branch mirror was not checked a second time.")
            self.assertEqual(unchanged_result.outcome, MirrorAutoSyncOutcome.UNCHANGED)
            download_bytes.assert_not_called()

    def test_automatic_branch_failure_keeps_previous_snapshot_and_retries_tomorrow(self) -> None:
        archive = self._zip_bytes({"project/startup.lua": b"print('published')\n"})
        published_revision = "a" * 40
        now = datetime(2026, 8, 6, 8, 0, tzinfo=UTC)
        with TemporaryDirectory() as temporary_name:
            service = MirrorService(Path(temporary_name) / "mirrors")
            with patch("mirror_service._utc_now_datetime", return_value=now):
                project = service.create_git_project(
                    project_id="project",
                    display_name="Project",
                    owner_user_id=42,
                    repository_url="https://github.com/example/project",
                )
            with (
                patch.object(service._git_client, "fetch_json", return_value={"sha": published_revision}),
                patch.object(service._git_client, "download_bytes", return_value=archive),
            ):
                published = service.refresh_project(
                    project_id=project.project_id,
                    actor_user_id=42,
                    can_manage_all=False,
                )

            service._replace_project(replace(published, next_check_at=(now - timedelta(seconds=1)).isoformat()))
            with (
                patch("mirror_service._utc_now_datetime", return_value=now),
                patch.object(service._git_client, "fetch_json", side_effect=MirrorError("Git provider is unavailable.")),
            ):
                result = service.sync_next_due_git_project(now=now)

            if result is None:
                self.fail("Due branch mirror was not checked.")
            self.assertEqual(result.outcome, MirrorAutoSyncOutcome.FAILED)
            self.assertTrue(result.project.is_snapshot_available)
            self.assertEqual(
                result.project.next_check_at,
                service._initial_auto_sync_time(project=published, now=now).isoformat(),
            )
            self.assertIsNotNone(service.manifest_path(project.project_id))

    def test_interrupted_publish_is_recovered_after_a_service_restart(self) -> None:
        with TemporaryDirectory() as temporary_name:
            storage_root = Path(temporary_name) / "mirrors"
            service = MirrorService(storage_root)
            project = service.create_git_project(
                project_id="project",
                display_name="Project",
                owner_user_id=42,
                repository_url="https://github.com/example/project",
            )
            service._replace_project(
                replace(
                    project,
                    sync_state=MirrorSyncState.PUBLISHING,
                    status_detail="Fetching source…",
                )
            )

            recovered_project = MirrorService(storage_root).get_project(project.project_id)

            if recovered_project is None:
                self.fail("Interrupted mirror project disappeared.")
            self.assertEqual(recovered_project.sync_state, MirrorSyncState.FAILED)
            self.assertIn("interrupted", recovered_project.status_detail or "")
            self.assertEqual(MirrorService(storage_root).get_project(project.project_id), recovered_project)

    def test_computercraft_installer_requests_revision_qualified_files(self) -> None:
        self.assertIn("?revision=", COMPUTERCRAFT_MIRROR_INSTALLER)
        self.assertIn(COMPUTERCRAFT_MIRROR_STATE_ROOT, COMPUTERCRAFT_MIRROR_INSTALLER)
        self.assertIn("--enable-startup", COMPUTERCRAFT_MIRROR_INSTALLER)
        self.assertIn("startup_dispatcher_script", COMPUTERCRAFT_MIRROR_INSTALLER)
        self.assertIn("automatic boot mode requires startup.lua", COMPUTERCRAFT_MIRROR_INSTALLER)
        self.assertIn("automatic boot mode requires an install directory other than /", COMPUTERCRAFT_MIRROR_INSTALLER)
        self.assertIn("program_startup_path", COMPUTERCRAFT_MIRROR_INSTALLER)
        self.assertIn("boot_updates_enabled", COMPUTERCRAFT_MIRROR_INSTALLER)
        self.assertIn("updater_script(boot_updates_enabled)", COMPUTERCRAFT_MIRROR_INSTALLER)
        self.assertIn("/startup.lua already exists", COMPUTERCRAFT_MIRROR_INSTALLER)
        self.assertNotIn("/.portal-mirrors", COMPUTERCRAFT_MIRROR_INSTALLER)
        self.assertNotIn(".portal-mirror", COMPUTERCRAFT_MIRROR_INSTALLER)
        self.assertIn("snapshot_changed", COMPUTERCRAFT_MIRROR_INSTALLER)
        self.assertIn("unmanaged file", COMPUTERCRAFT_MIRROR_INSTALLER)
        self.assertIn("wget", COMPUTERCRAFT_MIRROR_INSTALLER)

    def test_computercraft_setup_defaults_to_boot_updates_and_starts_the_project(self) -> None:
        project = MirrorProject(
            project_id="example",
            display_name="Example",
            owner_user_id=42,
            source=GitMirrorSource(
                host=MirrorGitHost.GITHUB,
                repository="example/project",
                tracking_mode=MirrorTrackingMode.BRANCH,
                ref="main",
            ),
        )

        automatic_command = ModWebMirrorsMixin._computercraft_install_command(project)
        manual_command = ModWebMirrorsMixin._computercraft_install_command(project, enable_startup=False)

        self.assertTrue(automatic_command.endswith("--enable-startup"))
        self.assertNotIn("--enable-startup", manual_command)
        self.assertIn("shell.run", COMPUTERCRAFT_MIRROR_INSTALLER)
        self.assertEqual(
            ModWebMirrorsMixin._computercraft_startup_snippet(project=project),
            f'pcall(function() shell.run("{COMPUTERCRAFT_MIRROR_STARTUP_DISPATCHER_PATH}") end)\n'
            'shell.run("/example/startup.lua")',
        )

    def test_malicious_archive_fails_without_a_public_snapshot(self) -> None:
        with TemporaryDirectory() as temporary_name:
            temporary_root = Path(temporary_name)
            archive_path = temporary_root / "malicious.zip"
            self._write_zip(archive_path, {"../outside.lua": b"nope\n"})
            service = MirrorService(temporary_root / "mirrors")
            project = service.create_upload_project(
                project_id="malicious",
                display_name="Malicious",
                owner_user_id=42,
                archive_path=archive_path,
            )

            with self.assertRaisesRegex(MirrorError, "unsafe archive member"):
                service.refresh_project(project_id=project.project_id, actor_user_id=42, can_manage_all=False)

            self.assertIsNone(service.manifest_path(project.project_id))
            failed_project = service.get_project(project.project_id)
            if failed_project is None:
                self.fail("Failed mirror project disappeared.")
            self.assertEqual(failed_project.sync_state, MirrorSyncState.FAILED)

    def test_only_owner_or_administrator_can_mutate_a_mirror(self) -> None:
        with TemporaryDirectory() as temporary_name:
            service = MirrorService(Path(temporary_name) / "mirrors")
            project = service.create_git_project(
                project_id="example",
                display_name="Example",
                owner_user_id=42,
                repository_url="https://gitlab.com/group/project",
            )

            with self.assertRaises(PermissionError):
                service.disable_project(project_id=project.project_id, actor_user_id=99, can_manage_all=False)
            disabled = service.disable_project(project_id=project.project_id, actor_user_id=99, can_manage_all=True)
            self.assertEqual(disabled.sync_state, MirrorSyncState.DISABLED)

    def test_git_repository_url_is_limited_to_supported_public_hosts(self) -> None:
        self.assertEqual(
            parse_public_git_repository_url("https://gitlab.com/group/nested/project"),
            ("gitlab", "group/nested/project"),
        )
        with self.assertRaisesRegex(MirrorError, "Only github.com and gitlab.com"):
            parse_public_git_repository_url("https://mirror.invalid/example/project")
        with self.assertRaisesRegex(MirrorError, "public HTTPS"):
            parse_public_git_repository_url("http://github.com/example/project")

    def test_repository_inspection_prefills_the_provider_default_branch(self) -> None:
        with TemporaryDirectory() as temporary_name:
            service = MirrorService(Path(temporary_name) / "mirrors")
            with patch.object(
                service._git_client,
                "fetch_json",
                side_effect=[
                    {"name": "Example Project", "default_branch": "main"},
                    {"sha": "b" * 40},
                ],
            ) as fetch_json:
                inspection = service.inspect_git_repository_url("https://github.com/example/example-project")

            self.assertEqual(inspection.display_name, "Example Project")
            self.assertEqual(inspection.suggested_project_id, "example-project")
            self.assertEqual(inspection.source.host, MirrorGitHost.GITHUB)
            self.assertEqual(inspection.source.tracking_mode, MirrorTrackingMode.BRANCH)
            self.assertEqual(inspection.source.ref, "main")
            self.assertEqual(inspection.default_branch, "main")
            self.assertEqual(fetch_json.call_args_list[1].args[0], "https://api.github.com/repos/example/example-project/commits/main")

    def test_commit_link_inspection_normalises_to_a_full_pinned_revision(self) -> None:
        with TemporaryDirectory() as temporary_name:
            service = MirrorService(Path(temporary_name) / "mirrors")
            with patch.object(
                service._git_client,
                "fetch_json",
                side_effect=[
                    {"name": "Project", "default_branch": "main"},
                    {"id": "c" * 40},
                ],
            ):
                inspection = service.inspect_git_repository_url(
                    "https://gitlab.com/group/nested/project/-/commit/abcdef1"
                )

            self.assertEqual(inspection.source.host, MirrorGitHost.GITLAB)
            self.assertEqual(inspection.source.repository, "group/nested/project")
            self.assertEqual(inspection.source.tracking_mode, MirrorTrackingMode.PINNED_COMMIT)
            self.assertEqual(inspection.source.ref, "c" * 40)

    def test_branch_and_commit_links_select_the_correct_tracking_mode(self) -> None:
        github_branch = parse_public_git_repository_link("https://github.com/example/project/tree/release")
        gitlab_commit = parse_public_git_repository_link(
            "https://gitlab.com/group/project/-/commit/abcdef1"
        )

        self.assertEqual(github_branch.tracking_mode, MirrorTrackingMode.BRANCH)
        self.assertEqual(github_branch.ref, "release")
        self.assertEqual(gitlab_commit.tracking_mode, MirrorTrackingMode.PINNED_COMMIT)
        self.assertEqual(gitlab_commit.ref, "abcdef1")

    def test_branch_links_allow_slashes_in_branch_names(self) -> None:
        github_branch = parse_public_git_repository_link("https://github.com/example/project/tree/release/1.0")
        gitlab_branch = parse_public_git_repository_link("https://gitlab.com/group/project/-/tree/release/1.0")

        self.assertEqual(github_branch.ref, "release/1.0")
        self.assertEqual(gitlab_branch.ref, "release/1.0")

    def test_git_reference_options_use_provider_branches_and_recent_commits(self) -> None:
        with TemporaryDirectory() as temporary_name:
            service = MirrorService(Path(temporary_name) / "mirrors")
            with patch.object(
                service._git_client,
                "fetch_json_list",
                side_effect=[
                    (
                        {"name": "main"},
                        {"name": "release/1.0"},
                    ),
                    (
                        {"id": "d" * 40, "title": "Release 1.0"},
                        {"id": "e" * 40, "title": "Fix startup"},
                    ),
                ],
            ) as fetch_json_list:
                branches = service.list_git_reference_options(
                    host=MirrorGitHost.GITHUB,
                    repository="example/project",
                    tracking_mode=MirrorTrackingMode.BRANCH,
                )
                commits = service.list_git_reference_options(
                    host=MirrorGitHost.GITLAB,
                    repository="group/nested/project",
                    tracking_mode=MirrorTrackingMode.PINNED_COMMIT,
                )

            self.assertEqual([option.ref for option in branches], ["main", "release/1.0"])
            self.assertEqual([option.label for option in commits], [f"{'d' * 12} · Release 1.0", f"{'e' * 12} · Fix startup"])
            self.assertEqual(
                fetch_json_list.call_args_list[0].args[0],
                "https://api.github.com/repos/example/project/branches?per_page=100",
            )
            self.assertEqual(
                fetch_json_list.call_args_list[1].args[0],
                "https://gitlab.com/api/v4/projects/group%2Fnested%2Fproject/repository/commits?per_page=50",
            )

    @staticmethod
    def _write_zip(path: Path, files: dict[str, bytes]) -> None:
        path.write_bytes(MirrorServiceCheck._zip_bytes(files))

    @staticmethod
    def _zip_bytes(files: dict[str, bytes]) -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(output, mode="w") as archive:
            for filename, contents in files.items():
                archive.writestr(filename, contents)
        return output.getvalue()


if __name__ == "__main__":
    unittest.main()

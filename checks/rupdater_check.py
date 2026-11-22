from __future__ import annotations

import io
import tarfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from subprocess import CalledProcessError
from tempfile import TemporaryDirectory
from unittest.mock import call, patch

import rupdater


class RupdaterSyncPlanTests(unittest.TestCase):
    def test_parse_changed_python_plan_rejects_empty_status_output(self) -> None:
        with self.assertRaisesRegex(RuntimeError, rupdater.NO_CHANGED_PYTHON_FILES_ERROR):
            rupdater.parse_changed_python_plan("")

    def test_select_sync_plan_returns_all_tracked_plan_when_requested(self) -> None:
        tracked_plan = rupdater.SyncPlan(write_files=(Path("main.py"),), delete_files=())

        with patch.object(rupdater, "tracked_python_sync_plan", return_value=tracked_plan):
            with patch.object(rupdater, "changed_python_plan") as changed_python_plan:
                selected_plan = rupdater.select_sync_plan(sync_all_tracked=True)

        self.assertIs(selected_plan, tracked_plan)
        changed_python_plan.assert_not_called()

    def test_select_sync_plan_falls_back_to_all_tracked_when_no_changes_exist(self) -> None:
        tracked_plan = rupdater.SyncPlan(write_files=(Path("main.py"),), delete_files=())
        stdout = io.StringIO()

        with patch.object(
            rupdater,
            "changed_python_plan",
            side_effect=RuntimeError(rupdater.NO_CHANGED_PYTHON_FILES_ERROR),
        ):
            with patch.object(rupdater, "tracked_python_sync_plan", return_value=tracked_plan):
                with redirect_stdout(stdout):
                    selected_plan = rupdater.select_sync_plan(sync_all_tracked=False)

        self.assertIs(selected_plan, tracked_plan)
        self.assertIn("falling back to syncing all tracked Python files", stdout.getvalue())

    def test_prompt_release_message_strips_input(self) -> None:
        with patch("builtins.input", return_value="  Release portal improvements  "):
            message = rupdater.prompt_release_message()

        self.assertEqual(message, "Release portal improvements")

    def test_prompt_release_message_rejects_blank_input(self) -> None:
        with patch("builtins.input", return_value="   "):
            with self.assertRaisesRegex(ValueError, "required"):
                rupdater.prompt_release_message()

    def test_commit_release_changes_skips_git_when_worktree_is_clean(self) -> None:
        with (
            patch.object(rupdater, "working_tree_status", return_value=""),
            patch.object(rupdater, "run_checked") as run_checked,
        ):
            committed = rupdater.commit_release_changes(message="Release portal improvements")

        self.assertFalse(committed)
        run_checked.assert_not_called()

    def test_commit_release_changes_stages_and_commits_dirty_worktree(self) -> None:
        with (
            patch.object(rupdater, "working_tree_status", side_effect=(" M web_dash/status_pages.py\n", "")),
            patch.object(rupdater, "run_checked") as run_checked,
        ):
            committed = rupdater.commit_release_changes(message="Release portal improvements")

        self.assertTrue(committed)
        self.assertEqual(
            run_checked.call_args_list,
            [
                call(["git", "add", "--all"], password=None),
                call(["git", "commit", "-m", "Release portal improvements"], password=None),
            ],
        )

    def test_build_deployment_metadata_uses_current_revision_and_tag(self) -> None:
        deployed_at = datetime(2026, 7, 31, 4, 30, tzinfo=timezone.utc)
        with (
            patch.object(rupdater, "release_revision", return_value="abcdef123456"),
            patch.object(rupdater, "release_version", return_value="v2026.07.31.1"),
        ):
            metadata = rupdater.build_deployment_metadata(
                target_name=rupdater.TargetName.PORTAL,
                source_files=(Path("rupdater.py"), Path("web_dash/status_pages.py")),
                now=deployed_at,
            )

        self.assertEqual(metadata.revision, "abcdef123456")
        self.assertEqual(metadata.version, "v2026.07.31.1")
        self.assertEqual(metadata.target_name, "portal")
        self.assertEqual(metadata.deployed_at, deployed_at)
        self.assertEqual(
            tuple(path.as_posix() for path in metadata.source_paths),
            ("rupdater.py", "web_dash/status_pages.py"),
        )


def _arguments(*, release: bool = False, check: bool = False, dry: bool = False, all_tracked: bool = False) -> Namespace:
    return Namespace(
        targets=["portal"], targets_file=Path("targets.json"), release=release, restart=release,
        check=check, dry=dry, all_tracked=all_tracked,
    )


def _frontend_fixture(root: Path) -> list[Path]:
    assets = root / "frontend" / "dist" / "assets"
    assets.mkdir(parents=True)
    files = {
        Path("frontend/package.json"): b"{}",
        Path("frontend/dist/index.html"): b'<script src="/mod-web/frontend/assets/app-new.js"></script>',
        Path("frontend/dist/assets/app-new.js"): b"export const ready = true;",
        Path("frontend/dist/assets/font.woff2"): b"\x00\xff\x01",
        Path("main.py"): b"",
        Path("pyproject.toml"): b"",
        Path("uv.lock"): b"",
    }
    for path, content in files.items():
        (root / path).write_bytes(content)
    node_modules = root / "frontend" / "node_modules"
    node_modules.mkdir()
    (node_modules / "development-only.js").write_text("not deployable")
    return sorted((path for path in files if path.is_relative_to(rupdater.FRONTEND_BUILD_DIRECTORY)), key=lambda path: path.as_posix())


class RupdaterFrontendTests(unittest.TestCase):
    def test_runtime_files_include_the_complete_build_and_exclude_dependencies(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            expected = _frontend_fixture(root)
            with patch.object(rupdater, "REPO_ROOT", root):
                self.assertEqual(rupdater.frontend_runtime_files(), expected)
                archive = rupdater.build_sync_archive(expected)
            with tarfile.open(fileobj=io.BytesIO(archive)) as built:
                font = built.extractfile("frontend/dist/assets/font.woff2")
                assert font is not None
                self.assertEqual(font.read(), b"\x00\xff\x01")

    def test_old_files_are_deleted_only_after_a_successful_transfer(self) -> None:
        target = rupdater.RemoteTarget(
            name=rupdater.TargetName.PORTAL, host="example.invalid", user="test",
            password=None, remote_root=PurePosixPath("/srv/yukibot"),
        )
        session = rupdater.RemoteSession(target=target, control_path=Path("/tmp/test-ssh"))
        for fails in (False, True):
            with self.subTest(transfer_fails=fails), TemporaryDirectory() as directory:
                root = Path(directory)
                _frontend_fixture(root)
                events: list[str] = []

                def sync(_session: rupdater.RemoteSession, _files: list[Path]) -> None:
                    events.append("transfer")
                    if fails:
                        raise RuntimeError("Transfer failed")

                with (
                    patch.object(rupdater, "REPO_ROOT", root),
                    patch.object(rupdater, "parse_args", return_value=_arguments()),
                    patch.object(rupdater, "require_program"),
                    patch.object(rupdater, "configured_targets", return_value=[target]),
                    patch.object(rupdater, "build_frontend"),
                    patch.object(rupdater, "select_sync_plan", return_value=rupdater.SyncPlan(
                        write_files=(Path("main.py"),), delete_files=(Path("frontend/dist/assets/old.js"),),
                    )),
                    patch.object(rupdater, "open_remote_session", return_value=session),
                    patch.object(rupdater, "require_remote_command_path"),
                    patch.object(rupdater, "require_remote_program"),
                    patch.object(rupdater, "sync_files", side_effect=sync),
                    patch.object(rupdater, "delete_remote_files", side_effect=lambda *_: events.append("delete")),
                    patch.object(rupdater, "sync_remote_dependencies"),
                    patch.object(rupdater, "close_remote_session") as close,
                    redirect_stdout(io.StringIO()),
                ):
                    if fails:
                        with self.assertRaisesRegex(RuntimeError, "Transfer failed"):
                            rupdater.main()
                    else:
                        self.assertEqual(rupdater.main(), 0)
                    close.assert_called_once_with(session)
                self.assertEqual(events, ["transfer"] if fails else ["transfer", "delete"])

    def test_frontend_is_optional_but_incomplete_builds_fail(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(rupdater, "REPO_ROOT", root),
                patch.object(rupdater, "run_checked") as run_checked,
            ):
                rupdater.build_frontend()
                self.assertEqual(rupdater.frontend_runtime_files(), [])
                run_checked.assert_not_called()
                (root / "frontend").mkdir()
                (root / "frontend/package.json").write_text("{}")
                with self.assertRaisesRegex(RuntimeError, "did not produce"):
                    rupdater.frontend_runtime_files()
                self.assertEqual(rupdater.frontend_runtime_files(allow_missing=True), [])

    def test_build_uses_locked_dependencies_and_stops_on_failure(self) -> None:
        with (
            patch.object(rupdater, "frontend_exists", return_value=True),
            patch.object(rupdater, "require_program") as require_program,
            patch.object(rupdater, "run_checked") as run_checked,
        ):
            rupdater.build_frontend()
            require_program.assert_called_once_with("pnpm")
            self.assertEqual(run_checked.call_args_list, [
                call(["pnpm", "--dir", "frontend", "install", "--frozen-lockfile"], password=None),
                call(["pnpm", "--dir", "frontend", "build"], password=None),
            ])
            run_checked.reset_mock()
            run_checked.side_effect = CalledProcessError(1, ["pnpm", "install"])
            with self.assertRaises(CalledProcessError):
                rupdater.build_frontend()
            self.assertEqual(run_checked.call_count, 1)

    def test_build_failure_cannot_commit_or_contact_a_remote(self) -> None:
        with (
            patch.object(rupdater, "parse_args", return_value=_arguments(release=True)),
            patch.object(rupdater, "require_program"),
            patch.object(rupdater, "configured_targets", return_value=[]),
            patch.object(rupdater, "build_frontend", side_effect=CalledProcessError(1, ["pnpm", "build"])),
            patch.object(rupdater, "commit_release_changes") as commit,
            patch.object(rupdater, "open_remote_session") as open_remote,
        ):
            with self.assertRaises(CalledProcessError):
                rupdater.main()
        commit.assert_not_called()
        open_remote.assert_not_called()

    def test_release_build_finishes_before_the_commit(self) -> None:
        events: list[str] = []
        with TemporaryDirectory() as directory:
            root = Path(directory)

            def build() -> None:
                _frontend_fixture(root)
                events.append("built")

            def stop_at_commit(*, message: str) -> bool:
                self.assertEqual(message, "Release frontend")
                self.assertTrue((root / "frontend/dist/assets/app-new.js").is_file())
                events.append("commit")
                raise RuntimeError("Stopped before committing in test")

            with (
                patch.object(rupdater, "REPO_ROOT", root),
                patch.object(rupdater, "parse_args", return_value=_arguments(release=True)),
                patch.object(rupdater, "require_program"),
                patch.object(rupdater, "configured_targets", return_value=[]),
                patch.object(rupdater, "build_frontend", side_effect=build),
                patch.object(rupdater, "working_tree_status", return_value=" M main.py\n"),
                patch.object(rupdater, "prompt_release_message", return_value="Release frontend"),
                patch.object(rupdater, "commit_release_changes", side_effect=stop_at_commit),
                patch.object(rupdater, "open_remote_session") as open_remote,
            ):
                with self.assertRaisesRegex(RuntimeError, "Stopped before committing"):
                    rupdater.main()
                open_remote.assert_not_called()
        self.assertEqual(events, ["built", "commit"])

    def test_release_rejects_assets_missing_from_the_committed_revision(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _frontend_fixture(root)
            with (
                patch.object(rupdater, "REPO_ROOT", root),
                patch.object(rupdater, "parse_args", return_value=_arguments(release=True)),
                patch.object(rupdater, "require_program"),
                patch.object(rupdater, "configured_targets", return_value=[]),
                patch.object(rupdater, "build_frontend"),
                patch.object(rupdater, "working_tree_status", return_value=""),
                patch.object(rupdater, "tracked_project_sync_plan", return_value=rupdater.SyncPlan(write_files=(Path("main.py"),), delete_files=())),
                patch.object(rupdater, "open_remote_session") as open_remote,
            ):
                with self.assertRaisesRegex(RuntimeError, "must be committed with the release"):
                    rupdater.main()
                open_remote.assert_not_called()

    def test_previews_include_assets_for_regular_and_all_tracked_updates_without_building(self) -> None:
        target = rupdater.RemoteTarget(
            name=rupdater.TargetName.PORTAL, host="example.invalid", user="test",
            password=None, remote_root=PurePosixPath("/srv/yukibot"),
        )
        for dry in (False, True):
            for all_tracked in (False, True):
                with self.subTest(dry=dry, all_tracked=all_tracked), TemporaryDirectory() as directory:
                    root = Path(directory)
                    expected = _frontend_fixture(root)
                    stdout = io.StringIO()
                    with (
                        patch.object(rupdater, "REPO_ROOT", root),
                        patch.object(rupdater, "parse_args", return_value=_arguments(check=not dry, dry=dry, all_tracked=all_tracked)),
                        patch.object(rupdater, "require_program"),
                        patch.object(rupdater, "configured_targets", return_value=[target]),
                        patch.object(rupdater, "select_sync_plan", return_value=rupdater.SyncPlan(write_files=(Path("main.py"),), delete_files=())),
                        patch.object(rupdater, "build_frontend") as build,
                        patch.object(rupdater, "print_check_plan") as print_plan,
                        patch.object(rupdater, "open_remote_session") as open_remote,
                        redirect_stdout(stdout),
                    ):
                        self.assertEqual(rupdater.main(), 0)
                    build.assert_not_called()
                    open_remote.assert_not_called()
                    if dry:
                        report = (root / "update_remotes_dry.txt").read_text()
                        for path in expected:
                            self.assertIn(path.as_posix(), report)
                        self.assertNotIn("node_modules", report)
                    else:
                        print_plan.assert_called_once()
                        files = print_plan.call_args.args[1]
                        self.assertTrue(set(expected).issubset(files))
                        self.assertTrue(all("node_modules" not in path.parts for path in files))
                    self.assertIn("Preview uses existing frontend assets", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()

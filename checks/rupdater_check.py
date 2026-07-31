from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()

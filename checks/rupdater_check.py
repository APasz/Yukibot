from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()

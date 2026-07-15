from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import restart_state
from restart_state import RestartKind


class RestartStateTests(unittest.TestCase):
    def test_update_bot_is_valid_pending_process_restart_kind(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            pending_path = temp_path / "pending_restart_type_sentinel.json"
            record_path = temp_path / "restart_type_sentinel.json"

            with (
                patch.object(restart_state, "PENDING_PROCESS_RESTART_KIND_PATH", pending_path),
                patch.object(restart_state, "PROCESS_RESTART_STATE_PATH", record_path),
            ):
                restart_state.mark_pending_process_restart(RestartKind.UPDATE_BOT)
                record = restart_state.record_process_start(datetime.fromtimestamp(1_782_909_000, timezone.utc))

                self.assertEqual(record.kind, RestartKind.UPDATE_BOT)
                self.assertFalse(pending_path.exists())

    def test_voice_restart_kind_is_not_valid_pending_process_restart_kind(self) -> None:
        with self.assertRaisesRegex(ValueError, "not a process restart kind"):
            restart_state.mark_pending_process_restart(RestartKind.MANUAL_VOICE)

if __name__ == "__main__":
    unittest.main()

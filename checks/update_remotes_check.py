from __future__ import annotations

import unittest
from pathlib import Path, PurePosixPath

import update_remotes


class UpdateRemotesTests(unittest.TestCase):
    def test_parse_tracked_python_files_returns_paths(self) -> None:
        files = update_remotes.parse_tracked_python_files("main.py\napps/_app.py\n")

        self.assertEqual(files, [Path("main.py"), Path("apps/_app.py")])

    def test_parse_tracked_python_files_rejects_empty_output(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "no tracked Python files"):
            update_remotes.parse_tracked_python_files("")

    def test_validate_rejects_placeholder_values(self) -> None:
        target = update_remotes.RemoteTarget(
            name=update_remotes.TargetName.WAKUSEI,
            host="wakusei.apasz.com",
            user=update_remotes.PLACEHOLDER_USER,
            password=update_remotes.PLACEHOLDER_PASSWORD,
            remote_root=update_remotes.PLACEHOLDER_REMOTE_ROOT,
        )

        with self.assertRaisesRegex(ValueError, "placeholder value"):
            target.validate()

    def test_validate_accepts_configured_values(self) -> None:
        target = update_remotes.RemoteTarget(
            name=update_remotes.TargetName.KOUSEI,
            host="kousei.apasz.com",
            user="bot",
            password="secret",
            remote_root=PurePosixPath("/srv/yukibot"),
            restart_command="systemctl --user restart yukibot",
        )

        target.validate()

    def test_dry_run_report_path_uses_target_names(self) -> None:
        report_path = update_remotes.dry_run_report_path()

        self.assertEqual(report_path.name, "update_remotes_dry.txt")

    def test_remote_parent_directories_include_nested_paths(self) -> None:
        target = update_remotes.RemoteTarget(
            name=update_remotes.TargetName.WAKUSEI,
            host="wakusei.apasz.com",
            user="bot",
            password="secret",
            remote_root=PurePosixPath("/srv/yukibot"),
        )
        files = [Path("main.py"), Path("apps/_app.py")]

        directories = update_remotes.remote_parent_directories(target, files)

        self.assertEqual(
            directories,
            [PurePosixPath("/srv/yukibot"), PurePosixPath("/srv/yukibot/apps")],
        )

    def test_remote_file_path_joins_relative_path(self) -> None:
        target = update_remotes.RemoteTarget(
            name=update_remotes.TargetName.WAKUSEI,
            host="wakusei.apasz.com",
            user="bot",
            password="secret",
            remote_root=PurePosixPath("/srv/yukibot"),
        )

        remote_path = update_remotes.remote_file_path(target, Path("apps/_app.py"))

        self.assertEqual(remote_path, PurePosixPath("/srv/yukibot/apps/_app.py"))

    def test_remote_command_path_is_absolute(self) -> None:
        self.assertEqual(update_remotes.REMOTE_MKDIR_PATH, "/bin/mkdir")
        self.assertEqual(update_remotes.REMOTE_CAT_PATH, "/bin/cat")

    def test_restart_delay_seconds_waits_for_kousei_after_wakusei(self) -> None:
        targets = [
            update_remotes.RemoteTarget(
                name=update_remotes.TargetName.WAKUSEI,
                host="wakusei.apasz.com",
                user="bot",
                password="secret",
                remote_root=PurePosixPath("/srv/yukibot"),
            ),
            update_remotes.RemoteTarget(
                name=update_remotes.TargetName.KOUSEI,
                host="kousei.apasz.com",
                user="bot",
                password="secret",
                remote_root=PurePosixPath("/srv/yukibot"),
            ),
        ]

        delay_seconds = update_remotes.restart_delay_seconds(targets[1], targets)

        self.assertEqual(delay_seconds, update_remotes.KOUSEI_RESTART_DELAY_SECONDS)

    def test_restart_delay_seconds_is_zero_for_single_target_restart(self) -> None:
        target = update_remotes.RemoteTarget(
            name=update_remotes.TargetName.KOUSEI,
            host="kousei.apasz.com",
            user="bot",
            password="secret",
            remote_root=PurePosixPath("/srv/yukibot"),
        )

        delay_seconds = update_remotes.restart_delay_seconds(target, [target])

        self.assertEqual(delay_seconds, 0)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from pathlib import Path, PurePosixPath

import rupdater


class UpdateRemotesTests(unittest.TestCase):
    def test_parse_tracked_python_files_returns_paths(self) -> None:
        files = rupdater.parse_tracked_python_files("main.py\napps/_app.py\n")

        self.assertEqual(files, [Path("main.py"), Path("apps/_app.py")])

    def test_parse_tracked_python_files_rejects_empty_output(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "no tracked Python files"):
            rupdater.parse_tracked_python_files("")

    def test_validate_rejects_placeholder_values(self) -> None:
        target = rupdater.RemoteTarget(
            name=rupdater.TargetName.WAKUSEI,
            host="wakusei.apasz.com",
            user=rupdater.PLACEHOLDER_USER,
            password=rupdater.PLACEHOLDER_PASSWORD,
            remote_root=rupdater.PLACEHOLDER_REMOTE_ROOT,
        )

        with self.assertRaisesRegex(ValueError, "placeholder value"):
            target.validate()

    def test_validate_accepts_configured_values(self) -> None:
        target = rupdater.RemoteTarget(
            name=rupdater.TargetName.KOUSEI,
            host="kousei.apasz.com",
            user="bot",
            password="secret",
            remote_root=PurePosixPath("/srv/yukibot"),
            restart_command="systemctl --user restart yukibot",
        )

        target.validate()

    def test_dry_run_report_path_uses_target_names(self) -> None:
        report_path = rupdater.dry_run_report_path()

        self.assertEqual(report_path.name, "update_remotes_dry.txt")

    def test_remote_parent_directories_include_nested_paths(self) -> None:
        target = rupdater.RemoteTarget(
            name=rupdater.TargetName.WAKUSEI,
            host="wakusei.apasz.com",
            user="bot",
            password="secret",
            remote_root=PurePosixPath("/srv/yukibot"),
        )
        files = [Path("main.py"), Path("apps/_app.py")]

        directories = rupdater.remote_parent_directories(target, files)

        self.assertEqual(
            directories,
            [PurePosixPath("/srv/yukibot"), PurePosixPath("/srv/yukibot/apps")],
        )

    def test_remote_file_path_joins_relative_path(self) -> None:
        target = rupdater.RemoteTarget(
            name=rupdater.TargetName.WAKUSEI,
            host="wakusei.apasz.com",
            user="bot",
            password="secret",
            remote_root=PurePosixPath("/srv/yukibot"),
        )

        remote_path = rupdater.remote_file_path(target, Path("apps/_app.py"))

        self.assertEqual(remote_path, PurePosixPath("/srv/yukibot/apps/_app.py"))

    def test_remote_command_path_is_absolute(self) -> None:
        self.assertEqual(rupdater.REMOTE_MKDIR_PATH, "/bin/mkdir")
        self.assertEqual(rupdater.REMOTE_CAT_PATH, "/bin/cat")

    def test_ssh_control_path_is_target_specific(self) -> None:
        wakusei_path = rupdater.ssh_control_path(
            rupdater.RemoteTarget(
                name=rupdater.TargetName.WAKUSEI,
                host="wakusei.apasz.com",
                user="bot",
                password="secret",
                remote_root=PurePosixPath("/srv/yukibot"),
            )
        )

        self.assertEqual(wakusei_path.name, "yukibot-wakusei.ssh")

    def test_restart_delay_seconds_waits_for_kousei_after_wakusei(self) -> None:
        targets = [
            rupdater.RemoteTarget(
                name=rupdater.TargetName.WAKUSEI,
                host="wakusei.apasz.com",
                user="bot",
                password="secret",
                remote_root=PurePosixPath("/srv/yukibot"),
            ),
            rupdater.RemoteTarget(
                name=rupdater.TargetName.KOUSEI,
                host="kousei.apasz.com",
                user="bot",
                password="secret",
                remote_root=PurePosixPath("/srv/yukibot"),
            ),
        ]

        delay_seconds = rupdater.restart_delay_seconds(targets[1], targets)

        self.assertEqual(delay_seconds, rupdater.KOUSEI_RESTART_DELAY_SECONDS)

    def test_restart_delay_seconds_is_zero_for_single_target_restart(self) -> None:
        target = rupdater.RemoteTarget(
            name=rupdater.TargetName.KOUSEI,
            host="kousei.apasz.com",
            user="bot",
            password="secret",
            remote_root=PurePosixPath("/srv/yukibot"),
        )

        delay_seconds = rupdater.restart_delay_seconds(target, [target])

        self.assertEqual(delay_seconds, 0)


if __name__ == "__main__":
    unittest.main()

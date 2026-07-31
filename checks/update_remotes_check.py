from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

import rupdater
from deployment_metadata import DeploymentMetadata
from restart_state import RestartKind


class UpdateRemotesTests(unittest.TestCase):
    def test_parse_tracked_python_files_returns_paths(self) -> None:
        files = rupdater.parse_tracked_python_files("main.py\napps/_app.py\n")

        self.assertEqual(files, [Path("main.py"), Path("apps/_app.py")])

    def test_parse_tracked_python_files_rejects_empty_output(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "no tracked Python files"):
            rupdater.parse_tracked_python_files("")

    def test_parse_tracked_project_files_returns_paths(self) -> None:
        files = rupdater.parse_tracked_project_files("main.py\nresources/logo.svg\n")

        self.assertEqual(files, [Path("main.py"), Path("resources/logo.svg")])

    def test_planned_sync_files_appends_required_project_files(self) -> None:
        files = rupdater.planned_sync_files([Path("main.py")])

        self.assertEqual(files, [Path("main.py"), Path("pyproject.toml"), Path("uv.lock")])

    def test_planned_sync_files_deduplicates_required_project_files(self) -> None:
        files = rupdater.planned_sync_files([Path("main.py"), Path("pyproject.toml")])

        self.assertEqual(files, [Path("main.py"), Path("pyproject.toml"), Path("uv.lock")])

    def test_parse_changed_python_files_returns_modified_and_untracked_paths(self) -> None:
        files = rupdater.parse_changed_python_files(" M main.py\n?? apps/_app.py\n")

        self.assertEqual(files, [Path("main.py"), Path("apps/_app.py")])

    def test_parse_changed_python_files_ignores_deleted_entries(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "no changed Python files"):
            rupdater.parse_changed_python_files(" D old_file.py\n")

    def test_parse_changed_python_files_ignores_non_python_entries(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "no changed Python files"):
            rupdater.parse_changed_python_files(" M remote_nodes.json\n?? resources/icon/root.jpg\n")

    def test_parse_changed_python_plan_tracks_deleted_python_files(self) -> None:
        plan = rupdater.parse_changed_python_plan(" D old_file.py\n M main.py\n")

        self.assertEqual(plan.write_files, (Path("main.py"),))
        self.assertEqual(plan.delete_files, (Path("old_file.py"),))

    def test_parse_changed_python_plan_tracks_renamed_python_files(self) -> None:
        plan = rupdater.parse_changed_python_plan("R  old_name.py -> main.py\n")

        self.assertEqual(plan.write_files, (Path("main.py"),))
        self.assertEqual(plan.delete_files, (Path("old_name.py"),))

    def test_validate_rejects_unsafe_remote_root(self) -> None:
        target = rupdater.RemoteTarget(
            name=rupdater.TargetName.WAKUSEI,
            host="wakusei.apasz.com",
            user="bot",
            password=None,
            remote_root=PurePosixPath("/"),
        )

        with self.assertRaisesRegex(ValueError, "non-root absolute path"):
            target.validate()

    def test_remote_targets_from_json_reads_all_target_settings(self) -> None:
        target_settings: dict[str, dict[str, object]] = {}
        for target_name in rupdater.TargetName:
            target_settings[target_name.value] = {
                "host": f"{target_name.value}.example.com",
                "user": "bot",
                "password": None,
                "remote_root": f"/srv/{target_name.value}",
                "restart_command": f"systemctl --user restart {target_name.value}",
            }
        target_settings["portal"]["password"] = "password"

        targets = rupdater.remote_targets_from_json(raw={"targets": target_settings})

        self.assertEqual(targets[rupdater.TargetName.PORTAL].user, "bot")
        self.assertEqual(targets[rupdater.TargetName.PORTAL].password, "password")
        self.assertIsNone(targets[rupdater.TargetName.WAKUSEI].password)
        self.assertEqual(targets[rupdater.TargetName.KOUSEI].remote_root, PurePosixPath("/srv/kousei"))

    def test_load_remote_targets_reads_json_file(self) -> None:
        target_settings = {
            target_name.value: {
                "host": f"{target_name.value}.example.com",
                "user": "bot",
                "password": None,
                "remote_root": f"/srv/{target_name.value}",
                "restart_command": f"systemctl --user restart {target_name.value}",
            }
            for target_name in rupdater.TargetName
        }
        with TemporaryDirectory() as temporary_directory:
            target_file = Path(temporary_directory) / "targets.json"
            target_file.write_text(json.dumps({"targets": target_settings}), encoding="utf-8")
            target_file.chmod(0o600)

            targets = rupdater.load_remote_targets(target_file=target_file)

        self.assertEqual(targets[rupdater.TargetName.PORTAL].host, "portal.example.com")

    @unittest.skipIf(os.name == "nt", "Unix permissions are not available on Windows.")
    def test_load_remote_targets_rejects_group_or_world_readable_configuration(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            target_file = Path(temporary_directory) / "targets.json"
            target_file.write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(PermissionError, "chmod 600"):
                rupdater.load_remote_targets(target_file=target_file)

    def test_remote_targets_from_json_rejects_missing_target(self) -> None:
        target_settings = {
            target_name.value: {
                "host": f"{target_name.value}.example.com",
                "user": "bot",
                "password": None,
                "remote_root": f"/srv/{target_name.value}",
                "restart_command": f"systemctl --user restart {target_name.value}",
            }
            for target_name in (rupdater.TargetName.WAKUSEI, rupdater.TargetName.KOUSEI)
        }
        with self.assertRaisesRegex(ValueError, "missing: portal"):
            rupdater.remote_targets_from_json(raw={"targets": target_settings})

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
        self.assertEqual(rupdater.REMOTE_SH_PATH, "/bin/sh")

    def test_build_remote_path_setup_command_adds_common_user_bin_dirs(self) -> None:
        command = rupdater.build_remote_path_setup_command()

        self.assertEqual(
            command,
            'export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin${PATH:+:$PATH}"; ',
        )

    def test_build_remote_shell_command_quotes_script_for_ssh(self) -> None:
        command = rupdater.build_remote_shell_command(
            'export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin${PATH:+:$PATH}"; command -v uv'
        )

        self.assertEqual(
            command,
            "/bin/sh -c 'export PATH=\"$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin${PATH:+:$PATH}\"; command -v uv'",
        )

    def test_build_remote_project_command_runs_from_target_root(self) -> None:
        target = rupdater.RemoteTarget(
            name=rupdater.TargetName.WAKUSEI,
            host="wakusei.apasz.com",
            user="bot",
            password="secret",
            remote_root=PurePosixPath("/srv/yukibot"),
        )

        command = rupdater.build_remote_project_command(target, rupdater.REMOTE_UV_SYNC_COMMAND)

        self.assertEqual(
            command,
            'export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin${PATH:+:$PATH}"; cd /srv/yukibot && uv sync',
        )

    def test_build_pending_restart_kind_write_command_writes_update_restart_sentinel(self) -> None:
        command = rupdater.build_pending_restart_kind_write_command(RestartKind.UPDATE_BOT)

        self.assertEqual(command, """printf '%s\\n' '{"kind": "update_bot"}' > pending_restart_type_sentinel.json""")

    def test_build_pending_restart_kind_write_command_rejects_voice_restart_kind(self) -> None:
        with self.assertRaisesRegex(ValueError, "not a process restart kind"):
            rupdater.build_pending_restart_kind_write_command(RestartKind.MANUAL_VOICE)

    def test_build_remote_restart_command_sets_update_restart_kind_before_restart(self) -> None:
        target = rupdater.RemoteTarget(
            name=rupdater.TargetName.WAKUSEI,
            host="wakusei.apasz.com",
            user="bot",
            password="secret",
            remote_root=PurePosixPath("/srv/yukibot"),
            restart_command="systemctl --user restart yukibot",
        )

        command = rupdater.build_remote_project_command(target, rupdater.build_remote_restart_command(target))

        self.assertEqual(
            command,
            'export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin${PATH:+:$PATH}"; '
            """cd /srv/yukibot && printf '%s\\n' '{"kind": "update_bot"}' > """
            "pending_restart_type_sentinel.json && systemctl --user restart yukibot",
        )

    def test_build_deployment_metadata_write_command_writes_ignored_project_file(self) -> None:
        metadata = DeploymentMetadata(
            revision="abcdef123456",
            deployed_at=datetime(2026, 7, 31, 4, 30, tzinfo=timezone.utc),
            target_name="portal",
            version="v2026.07.31.1",
        )

        command = rupdater.build_deployment_metadata_write_command(metadata)

        self.assertIn("/bin/mkdir -p -- .yukibot", command)
        self.assertIn(
            "deployment.json",
            command,
        )
        self.assertIn('"revision": "abcdef123456"', command)

    def test_stale_deployment_files_only_returns_paths_from_the_previous_manifest(self) -> None:
        deployed_at = datetime(2026, 7, 31, 4, 30, tzinfo=timezone.utc)
        previous = DeploymentMetadata(
            revision="abcdef123456",
            deployed_at=deployed_at,
            target_name="portal",
            source_paths=(PurePosixPath("old_module.py"), PurePosixPath("shared.py")),
        )
        current = DeploymentMetadata(
            revision="123456abcdef",
            deployed_at=deployed_at,
            target_name="portal",
            source_paths=(PurePosixPath("new_module.py"), PurePosixPath("shared.py")),
        )

        stale_files = rupdater.stale_deployment_files(previous=previous, current=current)

        self.assertEqual(stale_files, [Path("old_module.py")])

    def test_ssh_connection_options_support_ssh_agent_authentication(self) -> None:
        options = rupdater.ssh_connection_options(control_path=Path("/tmp/yukibot.ssh"), use_password=False)

        self.assertNotIn("PreferredAuthentications=password", options)
        self.assertNotIn("PubkeyAuthentication=no", options)

    def test_build_remote_command_path_check_command_fails_loudly_when_missing(self) -> None:
        command = rupdater.build_remote_command_path_check_command(rupdater.REMOTE_TAR_PATH)

        self.assertEqual(
            command,
            "[ -x /usr/bin/tar ] || { echo 'Required remote command is missing or not executable: /usr/bin/tar' >&2; exit 1; }",
        )

    def test_build_remote_program_check_command_fails_loudly_when_missing(self) -> None:
        command = rupdater.build_remote_program_check_command("uv")

        self.assertEqual(
            command,
            "export PATH=\"$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin${PATH:+:$PATH}\"; command -v uv >/dev/null 2>&1 || { echo 'Required program not found on remote PATH: uv' >&2; exit 1; }",
        )

    def test_ssh_control_path_is_target_specific(self) -> None:
        wakusei_path = rupdater.ssh_control_path(
            rupdater.RemoteTarget(
                name=rupdater.TargetName.WAKUSEI,
                host="wakusei.apasz.com",
                user="bot",
                password="secret",
                remote_root=PurePosixPath("/srv/yukibot"),
            ),
            "run-123",
        )

        self.assertEqual(wakusei_path.name, "yukibot-wakusei-run-123.ssh")

    def test_ordered_restart_targets_puts_yuki_first_and_portal_last(self) -> None:
        targets = [
            rupdater.RemoteTarget(
                name=rupdater.TargetName.PORTAL,
                host="wakusei.apasz.com",
                user="bot",
                password="secret",
                remote_root=PurePosixPath("/srv/portal"),
            ),
            rupdater.RemoteTarget(
                name=rupdater.TargetName.KOUSEI,
                host="kousei.apasz.com",
                user="bot",
                password="secret",
                remote_root=PurePosixPath("/srv/yukibot"),
            ),
            rupdater.RemoteTarget(
                name=rupdater.TargetName.WAKUSEI,
                host="wakusei.apasz.com",
                user="bot",
                password="secret",
                remote_root=PurePosixPath("/srv/yukibot"),
            ),
        ]

        ordered = rupdater.ordered_restart_targets(targets)

        self.assertEqual(
            [target.name for target in ordered],
            [rupdater.TargetName.WAKUSEI, rupdater.TargetName.KOUSEI, rupdater.TargetName.PORTAL],
        )

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

        self.assertEqual(delay_seconds, rupdater.RESTART_INTERVAL_SECONDS)

    def test_restart_delay_seconds_waits_between_each_ordered_target(self) -> None:
        targets = [
            rupdater.RemoteTarget(
                name=name,
                host="wakusei.apasz.com",
                user="bot",
                password="secret",
                remote_root=PurePosixPath(f"/srv/{name.value}"),
            )
            for name in (rupdater.TargetName.WAKUSEI, rupdater.TargetName.KOUSEI, rupdater.TargetName.PORTAL)
        ]

        delay_seconds = [rupdater.restart_delay_seconds(target, targets) for target in targets]

        self.assertEqual(delay_seconds, [0, rupdater.RESTART_INTERVAL_SECONDS, rupdater.RESTART_INTERVAL_SECONDS])

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

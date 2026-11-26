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

import config
import requests
import rupdater
from node_auth import NodeApiScope, verify_node_token


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


def _arguments(
    *,
    release: bool = False,
    restart: bool = False,
    no_auto_restart: bool = False,
    check: bool = False,
    dry: bool = False,
    all_tracked: bool = False,
) -> Namespace:
    return Namespace(
        targets=["portal"], targets_file=Path("targets.json"), release=release, restart=restart or release,
        no_auto_restart=no_auto_restart, check=check, dry=dry, all_tracked=all_tracked,
    )


class RupdaterRestartTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(TemporaryDirectory()))
        self.config_path = root / "configuration.json"
        config.save_bot_configuration(
            self.config_path,
            config.BotConfiguration(
                steamcmd_path="custom-steamcmd",
                restart_state=config.PersistedRestartState(auto_start_apps=("stale_app",)),
            ),
        )
        self.enterContext(patch.object(config, "BOT_CONFIGURATION_PATH", self.config_path))
        self.enterContext(patch.object(config, "ACTIVE_BOT_PROFILE", config.BOT_PROFILES[config.BotProfileName.YUKI]))
        self.enterContext(patch.object(config, "NODE_NAME", "test-node"))
        self.enterContext(patch.object(config, "NODE_API_TOKEN_SECRET", "test-secret"))
        self.enterContext(patch.object(config, "LOCAL_NODE_API_BASE_URL", "http://127.0.0.1:8082/api/node"))
        self.enterContext(patch.object(rupdater.time, "time", return_value=1000))
        session = self.enterContext(patch("requests.Session"))
        self.client = session.return_value.__enter__.return_value
        self.client.get.return_value.json.return_value = {"running_app_ids": ["minecraft_alpha", "factorio_alpha"]}
        self.enterContext(redirect_stdout(io.StringIO()))

    def test_restart_flag_restores_apps_by_default_and_supports_opt_out(self) -> None:
        for no_auto_restart in (False, True):
            with self.subTest(no_auto_restart=no_auto_restart):
                arguments = ["rupdater.py", "portal", "--restart"]
                if no_auto_restart:
                    arguments.append("--no-auto-restart")
                with patch("sys.argv", arguments):
                    args = rupdater.parse_args()
                self.assertTrue(args.restart)
                self.assertEqual(args.no_auto_restart, no_auto_restart)

    def test_prepare_restart_saves_running_app_ids_and_authenticates_locally(self) -> None:
        app_names = rupdater.prepare_restart_auto_start_apps(enabled=True)

        self.assertEqual(app_names, ("minecraft_alpha", "factorio_alpha"))
        loaded = config.load_bot_configuration(self.config_path)
        self.assertEqual(loaded.restart_state.auto_start_apps, app_names)
        self.assertEqual(loaded.steamcmd_path, "custom-steamcmd")
        self.assertFalse(self.client.trust_env)
        self.client.get.return_value.raise_for_status.assert_called_once()
        request = self.client.get.call_args
        self.assertEqual(request.args, ("http://127.0.0.1:8082/api/node/system",))
        self.assertEqual(request.kwargs["timeout"], rupdater.RESTART_SNAPSHOT_TIMEOUT_SECONDS)
        token = request.kwargs["headers"]["Authorization"].removeprefix("Bearer ")
        grant = verify_node_token(
            secret="test-secret", token=token, node="test-node", app=None,
            required_scopes=(NodeApiScope.APPS_READ,), now=1000,
        )
        self.assertEqual(grant.scopes, frozenset({NodeApiScope.APPS_READ}))

    def test_empty_running_list_clears_stale_restart_apps(self) -> None:
        self.client.get.return_value.json.return_value = {"running_app_ids": []}

        self.assertEqual(rupdater.prepare_restart_auto_start_apps(enabled=True), ())
        self.assertEqual(config.load_bot_configuration(self.config_path).restart_state.auto_start_apps, ())

    def test_opt_out_and_portal_clear_restart_apps_without_querying_the_api(self) -> None:
        for portal in (False, True):
            with self.subTest(portal=portal):
                profile = config.BotProfileName.PORTAL if portal else config.BotProfileName.YUKI
                config.save_restart_auto_start_apps(self.config_path, ("stale_app",))
                with patch.object(config, "ACTIVE_BOT_PROFILE", config.BOT_PROFILES[profile]):
                    self.assertEqual(rupdater.prepare_restart_auto_start_apps(enabled=portal), ())
                self.client.get.assert_not_called()
                self.assertEqual(config.load_bot_configuration(self.config_path).restart_state.auto_start_apps, ())

    def test_failed_capture_preserves_state_and_raises(self) -> None:
        self.client.get.side_effect = requests.ConnectionError("Node API unavailable")

        with self.assertRaisesRegex(requests.ConnectionError, "Node API unavailable"):
            rupdater.prepare_restart_auto_start_apps(enabled=True)
        self.assertEqual(config.load_bot_configuration(self.config_path).restart_state.auto_start_apps, ("stale_app",))

    def test_invalid_or_missing_running_list_is_rejected(self) -> None:
        for payload in ({}, {"running_app_ids": "minecraft_alpha"}, {"running_app_ids": [None]}):
            with self.subTest(payload=payload):
                self.client.get.return_value.json.return_value = payload
                with self.assertRaisesRegex(ValueError, "running_app_ids"):
                    rupdater.prepare_restart_auto_start_apps(enabled=True)
                self.assertEqual(config.load_bot_configuration(self.config_path).restart_state.auto_start_apps, ("stale_app",))


def _frontend_fixture(root: Path) -> list[Path]:
    assets = root / "frontend" / "dist" / "assets"
    assets.mkdir(parents=True)
    files = {
        Path("frontend/package.json"): b"{}",
        Path("frontend/dist/index.html"): b'<script src="/mod-web/frontend/assets/app-new.js"></script>',
        Path("frontend/dist/assets/app-new.js"): b"export const ready = true;",
        Path("frontend/dist/assets/font.woff2"): b"\x00\xff\x01",
        Path("main.py"): b"",
        Path("rupdater.py"): b"",
        Path("config.py"): b"",
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
        for fails, restart, no_auto_restart in (
            (False, False, False), (True, True, False), (False, True, False), (False, True, True),
        ):
            with self.subTest(transfer_fails=fails, restart=restart, no_auto_restart=no_auto_restart), TemporaryDirectory() as directory:
                root = Path(directory)
                _frontend_fixture(root)
                events: list[str] = []

                def sync(_session: rupdater.RemoteSession, _files: list[Path]) -> None:
                    events.append("transfer")
                    if fails:
                        raise RuntimeError("Transfer failed")

                with (
                    patch.object(rupdater, "REPO_ROOT", root),
                    patch.object(rupdater, "parse_args", return_value=_arguments(restart=restart, no_auto_restart=no_auto_restart)),
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
                    patch.object(rupdater, "restart_remote") as restart_remote,
                    patch.object(rupdater, "close_remote_session") as close,
                    redirect_stdout(io.StringIO()),
                ):
                    if fails:
                        with self.assertRaisesRegex(RuntimeError, "Transfer failed"):
                            rupdater.main()
                    else:
                        self.assertEqual(rupdater.main(), 0)
                    close.assert_called_once_with(session)
                    if restart and not fails:
                        restart_remote.assert_called_once_with(session, auto_restart_apps=not no_auto_restart)
                    else:
                        restart_remote.assert_not_called()
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

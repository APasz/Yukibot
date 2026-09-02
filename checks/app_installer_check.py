from __future__ import annotations

import asyncio
import logging
import unittest
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI, HTTPException, Request
from httpx import ASGITransport, AsyncClient, Response

import config
from _manager import AppInstallInput, AppInstanceCreateRequest, AppInstanceCreationPlan, AppSteamInstallRecipe
from _security import Power_Level
from apps._config import AppVersion, SteamUpdateBranch, SteamUpdateConfig, SteamUpdateLogin
from node_api.app_installer import (
    NodeAppInstallCatalog,
    NodeAppInstallRequest,
    NodeAppInstallScopeOption,
    NodeAppInstallerService,
    NodeAppInstallerSettingsMutationResult,
    NodeAppInstallerSettingsState,
    NodeAppInstallState,
    NodeAppInstallStatus,
    _NodeAppInstallJob,
)
from node_api.app_installer_routes import register_app_installer_routes
from node_api.node_routes import register_node_management_routes
from node_auth import NodeAccessGrant, NodeApiScope


class _InstallerManager:
    def __init__(self, *, root: Path) -> None:
        self.recipe = AppSteamInstallRecipe(
            scope="demo",
            label="Demo App",
            default_port=25565,
            steam_update=SteamUpdateConfig(
                app_id=123,
                branches=(SteamUpdateBranch(branch_id="public", label="Stable"),),
                selected_branch="public",
            ),
            inputs=(AppInstallInput.ADMIN_PASSWORD,),
        )
        self.plan = AppInstanceCreationPlan(
            scope="demo",
            instance_key="alpha",
            friendly_name="Demo Alpha",
            subfolder=Path("demo-alpha"),
            directory=root / "demo-alpha",
            server_log_file=None,
            admin_password="secret",
            steam_branch="public",
            scope_path=root / "apps" / "demo",
            instances_path=root / "apps" / "demo" / "instances.json",
        )
        self.create_requests: list[AppInstanceCreateRequest] = []
        self.loaded_instances: list[tuple[str, str]] = []
        self.discarded_instances: list[tuple[str, str]] = []

    def list_steam_install_recipes(self) -> tuple[AppSteamInstallRecipe, ...]:
        return (self.recipe,)

    def prepare_instance_creation(self, request: AppInstanceCreateRequest) -> AppInstanceCreationPlan:
        assert request.scope == "demo"
        assert request.instance_key == self.plan.instance_key
        assert request.steam_branch == "public"
        assert request.initial_version == AppVersion(main="0.0")
        return self.plan

    def create_instance(self, request: AppInstanceCreateRequest) -> str:
        self.create_requests.append(request)
        return "demo_alpha"

    async def load_instance(self, *, scope: str, instance_key: str) -> None:
        self.loaded_instances.append((scope, instance_key))

    def discard_unloaded_instance(self, *, scope: str, instance_key: str) -> None:
        self.discarded_instances.append((scope, instance_key))


class _DenyAppInstallScopePolicy:
    def allows(self, scope: str) -> bool:
        del scope
        return False


_DENY_APP_INSTALL_SCOPE_POLICY = _DenyAppInstallScopePolicy()


class _RouteService:
    node_name = "node-a"

    def __init__(self) -> None:
        self.access_requests: list[tuple[str | None, tuple[NodeApiScope, ...]]] = []
        self.start_requests: list[tuple[NodeAppInstallRequest, int]] = []
        self.start_error: Exception | None = None

    def _require_access(
        self,
        request: Request,
        access_token: str | None,
        *,
        app_name: str | None,
        scopes: tuple[NodeApiScope, ...],
        token_node_names: Sequence[str] | None = None,
    ) -> NodeAccessGrant | None:
        del request, access_token, token_node_names
        self.access_requests.append((app_name, scopes))
        return None

    def _request_actor_user_id(
        self,
        *,
        request: Request,
        access_token: str | None,
        app_name: str | None,
        scopes: tuple[NodeApiScope, ...],
        verified_grant: NodeAccessGrant | None = None,
    ) -> int:
        del request, access_token, app_name, scopes
        assert verified_grant is None
        return 42

    async def build_app_install_catalog(self) -> NodeAppInstallCatalog:
        return NodeAppInstallCatalog(node=self.node_name, recipes=())

    async def start_app_install(self, *, request: NodeAppInstallRequest, actor_user_id: int) -> NodeAppInstallStatus:
        if self.start_error is not None:
            raise self.start_error
        self.start_requests.append((request, actor_user_id))
        return NodeAppInstallStatus(
            job_id="job-1",
            node=self.node_name,
            scope=request.scope,
            state=NodeAppInstallState.QUEUED,
            summary="Queued.",
        )

    def app_install_status(self, *, job_id: str) -> NodeAppInstallStatus:
        if job_id != "job-1":
            raise LookupError
        return NodeAppInstallStatus(
            job_id=job_id,
            node=self.node_name,
            scope="demo",
            state=NodeAppInstallState.READY,
            summary="Installed.",
        )


class _NodeSettingsRouteService(_RouteService):
    def __init__(self) -> None:
        super().__init__()
        self.settings = config.AppInstallerSettings(allowed_scopes=("demo",))
        self.mutation_requests: list[tuple[config.AppInstallerSettings, int]] = []

    def read_app_installer_settings(self) -> NodeAppInstallerSettingsState:
        return NodeAppInstallerSettingsState(
            node=self.node_name,
            settings=self.settings,
            available_apps=(NodeAppInstallScopeOption(scope="demo", label="Demo App"),),
        )

    async def mutate_app_installer_settings(
        self,
        *,
        settings: config.AppInstallerSettings,
        actor_user_id: int,
    ) -> NodeAppInstallerSettingsMutationResult:
        self.settings = settings
        self.mutation_requests.append((settings, actor_user_id))
        return NodeAppInstallerSettingsMutationResult(
            node=self.node_name,
            message="Updated app install settings for node-a.",
            settings=settings,
        )


class AppInstallerCheck(unittest.TestCase):
    def test_completed_install_jobs_have_bounded_history(self) -> None:
        service = NodeAppInstallerService(node_name=lambda: "node-a", invalidate_state_caches=Mock())
        service._jobs = {
            f"job-{index}": _NodeAppInstallJob(
                status=NodeAppInstallStatus(
                    job_id=f"job-{index}",
                    node="node-a",
                    scope="demo",
                    state=NodeAppInstallState.FAILED,
                    summary="Install failed.",
                ),
                staging_directory=Path(f"/tmp/staging-{index}"),
            )
            for index in range(3)
        }

        with patch("node_api.app_installer._INSTALL_COMPLETED_JOB_LIMIT", 2):
            with service._lock:
                service._prune_completed_jobs_locked()

        self.assertEqual(tuple(service._jobs), ("job-1", "job-2"))

    def test_catalog_exposes_recipe_fields_without_steam_credentials(self) -> None:
        with TemporaryDirectory() as temp_dir:
            manager = _InstallerManager(root=Path(temp_dir))
            manager.recipe = AppSteamInstallRecipe(
                scope=manager.recipe.scope,
                label=manager.recipe.label,
                default_port=manager.recipe.default_port,
                steam_update=SteamUpdateConfig(
                    app_id=123,
                    login=SteamUpdateLogin(username="account", password="not-disclosed"),
                    branches=(SteamUpdateBranch(branch_id="public", label="Stable"),),
                    selected_branch="public",
                ),
                inputs=manager.recipe.inputs,
            )
            service = NodeAppInstallerService(node_name=lambda: "node-a", invalidate_state_caches=Mock())

            catalog = asyncio.run(service.build_catalog(manager=manager))

        self.assertEqual(catalog.node, "node-a")
        self.assertEqual(catalog.recipes[0].scope, "demo")
        self.assertEqual(catalog.recipes[0].fields[0].key, AppInstallInput.ADMIN_PASSWORD.value)
        self.assertIn("claiming it in the game client", catalog.recipes[0].fields[0].help_text or "")
        self.assertNotIn("not-disclosed", str(catalog.to_mapping()))

    def test_catalog_fetches_live_branches_and_keeps_configured_overrides(self) -> None:
        async def _build_catalog() -> tuple[NodeAppInstallCatalog, AsyncMock]:
            with TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                manager = _InstallerManager(root=root)
                manager.recipe = replace(
                    manager.recipe,
                    scope="sevendays",
                    steam_update=SteamUpdateConfig(
                        app_id=294420,
                        branches=(
                            SteamUpdateBranch(branch_id="public", label="Preferred"),
                            SteamUpdateBranch(branch_id="private", beta_password="secret"),
                        ),
                        selected_branch="public",
                    ),
                )
                service = NodeAppInstallerService(node_name=lambda: "node-a", invalidate_state_caches=Mock())
                branch_loader = AsyncMock(
                    return_value=(
                        SteamUpdateBranch(branch_id="public", label="Stable"),
                        SteamUpdateBranch(branch_id="experimental", label="Experimental"),
                    )
                )
                with (
                    patch.object(config, "DIR_LOG", root / "logs"),
                    patch("node_api.app_installer.load_steam_update_branches", new=branch_loader),
                    patch("node_api.app_installer.resolve_steamcmd_command_prefix", return_value=("steamcmd",)),
                ):
                    catalog = await service.build_catalog(manager=manager)
            return catalog, branch_loader

        catalog, branch_loader = asyncio.run(_build_catalog())

        self.assertEqual(
            [(branch.branch_id, branch.label) for branch in catalog.recipes[0].branches],
            [
                ("public", "Preferred"),
                ("experimental", "Experimental"),
                ("private", "private"),
            ],
        )
        branch_loader.assert_awaited_once()

    def test_node_allowlist_filters_catalog_and_rejects_direct_installs(self) -> None:
        async def _run() -> None:
            with TemporaryDirectory() as temp_dir:
                manager = _InstallerManager(root=Path(temp_dir))
                acl = Mock()
                acl.perm_check = AsyncMock()
                service = NodeAppInstallerService(
                    node_name=lambda: "node-a",
                    invalidate_state_caches=Mock(),
                    scope_policy=lambda: _DENY_APP_INSTALL_SCOPE_POLICY,
                )
                request = NodeAppInstallRequest(
                    scope="demo",
                    instance_key="alpha",
                    friendly_name="Demo Alpha",
                    subfolder="demo-alpha",
                    steam_branch_id="public",
                    inputs={AppInstallInput.ADMIN_PASSWORD: "secret"},
                )

                self.assertEqual((await service.build_catalog(manager=manager)).recipes, ())
                with self.assertRaisesRegex(ValueError, "not available for installation"):
                    await service.start_install(
                        manager=manager,
                        acl=acl,
                        actor_user_id=42,
                        request=request,
                    )

        asyncio.run(_run())

    def test_install_stages_files_then_registers_and_loads_instance(self) -> None:
        async def _run() -> None:
            with TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                manager = _InstallerManager(root=root)
                acl = Mock()
                acl.perm_check = AsyncMock()
                invalidated = Mock()
                service = NodeAppInstallerService(node_name=lambda: "node-a", invalidate_state_caches=invalidated)
                request = NodeAppInstallRequest(
                    scope="demo",
                    instance_key="alpha",
                    friendly_name="Demo Alpha",
                    subfolder="demo-alpha",
                    steam_branch_id="public",
                    inputs={AppInstallInput.ADMIN_PASSWORD: "secret"},
                )

                async def _fake_steamcmd(*, command: list[str], cwd: Path, on_output: object = None) -> bool:
                    del command
                    if callable(on_output):
                        on_output("stdout", "Success! App 123 fully installed.")
                    (cwd / "installed.txt").write_text("ok", encoding="utf-8")
                    return True

                with patch("node_api.app_installer.run_steamcmd_command", new=_fake_steamcmd):
                    queued = await service.start_install(
                        manager=manager,
                        acl=acl,
                        actor_user_id=42,
                        request=request,
                    )
                    for _ in range(20):
                        status = service.install_status(job_id=queued.job_id)
                        if not status.running:
                            break
                        await asyncio.sleep(0)
                    else:
                        self.fail("Install task did not finish.")

                status = service.install_status(job_id=queued.job_id)
                self.assertEqual(status.state, NodeAppInstallState.READY)
                self.assertEqual(status.app_name, "demo_alpha")
                self.assertTrue((root / "demo-alpha" / "installed.txt").is_file())
                self.assertEqual(manager.loaded_instances, [("demo", "alpha")])
                self.assertEqual(manager.create_requests[0].admin_password, "secret")
                self.assertEqual(manager.create_requests[0].steam_branch, "public")
                self.assertEqual(manager.create_requests[0].initial_version, AppVersion(main="0.0"))
                acl.perm_check.assert_awaited_once_with(42, Power_Level.sudo)
                invalidated.assert_called_once_with()

        asyncio.run(_run())

    def test_missing_recipe_input_fails_before_starting_a_job(self) -> None:
        async def _run() -> None:
            with TemporaryDirectory() as temp_dir:
                manager = _InstallerManager(root=Path(temp_dir))
                acl = Mock()
                acl.perm_check = AsyncMock()
                service = NodeAppInstallerService(node_name=lambda: "node-a", invalidate_state_caches=Mock())
                request = NodeAppInstallRequest(
                    scope="demo",
                    instance_key="alpha",
                    friendly_name="Demo Alpha",
                    subfolder="demo-alpha",
                    steam_branch_id="public",
                )

                with self.assertRaisesRegex(ValueError, "Admin password is required"):
                    await service.start_install(
                        manager=manager,
                        acl=acl,
                        actor_user_id=42,
                        request=request,
                    )

                self.assertEqual(manager.create_requests, [])

        asyncio.run(_run())

    def test_failed_install_redacts_steam_and_recipe_secrets(self) -> None:
        async def _run() -> None:
            with TemporaryDirectory() as temp_dir:
                manager = _InstallerManager(root=Path(temp_dir))
                manager.recipe = replace(
                    manager.recipe,
                    steam_update=SteamUpdateConfig(
                        app_id=123,
                        login=SteamUpdateLogin(username="account", password="steam-secret"),
                        branches=(
                            SteamUpdateBranch(
                                branch_id="public",
                                label="Stable",
                                beta_password="beta-secret",
                            ),
                        ),
                        selected_branch="public",
                    ),
                )
                acl = Mock()
                acl.perm_check = AsyncMock()
                service = NodeAppInstallerService(node_name=lambda: "node-a", invalidate_state_caches=Mock())
                request = NodeAppInstallRequest(
                    scope="demo",
                    instance_key="alpha",
                    friendly_name="Demo Alpha",
                    subfolder="demo-alpha",
                    steam_branch_id="public",
                    inputs={AppInstallInput.ADMIN_PASSWORD: "admin-secret"},
                )

                async def _failed_steamcmd(*, command: list[str], cwd: Path, on_output: object = None) -> bool:
                    del command, cwd, on_output
                    raise RuntimeError("steam-secret beta-secret admin-secret")

                with patch("node_api.app_installer.run_steamcmd_command", new=_failed_steamcmd):
                    queued = await service.start_install(
                        manager=manager,
                        acl=acl,
                        actor_user_id=42,
                        request=request,
                    )
                    for _ in range(20):
                        status = service.install_status(job_id=queued.job_id)
                        if not status.running:
                            break
                        await asyncio.sleep(0)
                    else:
                        self.fail("Install task did not finish.")

                status = service.install_status(job_id=queued.job_id)
                self.assertEqual(status.state, NodeAppInstallState.FAILED)
                self.assertEqual(status.detail, "****** ****** ******")

        asyncio.run(_run())

    def test_cleanup_failure_releases_the_install_reservation(self) -> None:
        async def _run() -> None:
            with TemporaryDirectory() as temp_dir:
                manager = _InstallerManager(root=Path(temp_dir))
                acl = Mock()
                acl.perm_check = AsyncMock()
                service = NodeAppInstallerService(node_name=lambda: "node-a", invalidate_state_caches=Mock())
                request = NodeAppInstallRequest(
                    scope="demo",
                    instance_key="alpha",
                    friendly_name="Demo Alpha",
                    subfolder="demo-alpha",
                    steam_branch_id="public",
                    inputs={AppInstallInput.ADMIN_PASSWORD: "secret"},
                )

                async def fake_steamcmd(*, command: list[str], cwd: Path, on_output: object = None) -> bool:
                    del command, cwd, on_output
                    return False

                with (
                    patch("node_api.app_installer.run_steamcmd_command", new=fake_steamcmd),
                    patch(
                        "node_api.app_installer.run_blocking",
                        new=AsyncMock(side_effect=RuntimeError("cleanup failed")),
                    ),
                ):
                    first = await service.start_install(
                        manager=manager,
                        acl=acl,
                        actor_user_id=42,
                        request=request,
                    )
                    first_task = service._jobs[first.job_id].task
                    self.assertIsNotNone(first_task)
                    assert first_task is not None
                    with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                        await first_task

                    self.assertFalse(service._active_targets)
                    self.assertFalse(service._active_instance_keys)
                    second = await service.start_install(
                        manager=manager,
                        acl=acl,
                        actor_user_id=42,
                        request=request,
                    )
                    second_task = service._jobs[second.job_id].task
                    self.assertIsNotNone(second_task)
                    assert second_task is not None
                    with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                        await second_task

        asyncio.run(_run())

    def test_cancelled_install_after_promotion_invalidates_state_caches(self) -> None:
        async def _run() -> None:
            with TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                manager = _InstallerManager(root=root)
                acl = Mock()
                acl.perm_check = AsyncMock()
                invalidated = Mock()
                service = NodeAppInstallerService(node_name=lambda: "node-a", invalidate_state_caches=invalidated)
                request = NodeAppInstallRequest(
                    scope="demo",
                    instance_key="alpha",
                    friendly_name="Demo Alpha",
                    subfolder="demo-alpha",
                    steam_branch_id="public",
                    inputs={AppInstallInput.ADMIN_PASSWORD: "secret"},
                )
                loading_started = asyncio.Event()

                async def _loading_instance(*, scope: str, instance_key: str) -> None:
                    manager.loaded_instances.append((scope, instance_key))
                    loading_started.set()
                    await asyncio.Event().wait()

                async def _fake_steamcmd(*, command: list[str], cwd: Path, on_output: object = None) -> bool:
                    del command, on_output
                    (cwd / "installed.txt").write_text("ok", encoding="utf-8")
                    return True

                manager.load_instance = _loading_instance  # type: ignore[method-assign]
                with patch("node_api.app_installer.run_steamcmd_command", new=_fake_steamcmd):
                    queued = await service.start_install(
                        manager=manager,
                        acl=acl,
                        actor_user_id=42,
                        request=request,
                    )
                    await loading_started.wait()
                    service.cancel_pending()
                    for _ in range(20):
                        status = service.install_status(job_id=queued.job_id)
                        if not status.running:
                            break
                        await asyncio.sleep(0)
                    else:
                        self.fail("Cancelled install task did not finish.")

                status = service.install_status(job_id=queued.job_id)
                self.assertEqual(status.state, NodeAppInstallState.READY)
                self.assertEqual(status.summary, "Installed. Restart to load it.")
                invalidated.assert_called_once_with()

        asyncio.run(_run())

    def test_concurrent_installs_allow_the_same_default_port(self) -> None:
        async def _run() -> None:
            with TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                first_manager = _InstallerManager(root=root)
                second_manager = _InstallerManager(root=root)
                second_manager.plan = replace(
                    second_manager.plan,
                    instance_key="beta",
                    friendly_name="Demo Beta",
                    subfolder=Path("demo-beta"),
                    directory=root / "demo-beta",
                )
                acl = Mock()
                acl.perm_check = AsyncMock()
                service = NodeAppInstallerService(node_name=lambda: "node-a", invalidate_state_caches=Mock())
                first_request = NodeAppInstallRequest(
                    scope="demo",
                    instance_key="alpha",
                    friendly_name="Demo Alpha",
                    subfolder="demo-alpha",
                    steam_branch_id="public",
                    inputs={AppInstallInput.ADMIN_PASSWORD: "secret"},
                )
                second_request = first_request.model_copy(
                    update={
                        "instance_key": "beta",
                        "friendly_name": "Demo Beta",
                        "subfolder": "demo-beta",
                    }
                )
                both_steamcmd_started = asyncio.Event()
                allow_steamcmd_finish = asyncio.Event()
                steamcmd_started_count = 0

                async def _blocked_steamcmd(*, command: list[str], cwd: Path, on_output: object = None) -> bool:
                    nonlocal steamcmd_started_count
                    del command, on_output
                    steamcmd_started_count += 1
                    if steamcmd_started_count == 2:
                        both_steamcmd_started.set()
                    await allow_steamcmd_finish.wait()
                    (cwd / "installed.txt").write_text("ok", encoding="utf-8")
                    return True

                with patch("node_api.app_installer.run_steamcmd_command", new=_blocked_steamcmd):
                    queued = await service.start_install(
                        manager=first_manager,
                        acl=acl,
                        actor_user_id=42,
                        request=first_request,
                    )
                    second_queued = await service.start_install(
                        manager=second_manager,
                        acl=acl,
                        actor_user_id=42,
                        request=second_request,
                    )
                    await both_steamcmd_started.wait()
                    allow_steamcmd_finish.set()
                    for _ in range(20):
                        statuses = (
                            service.install_status(job_id=queued.job_id),
                            service.install_status(job_id=second_queued.job_id),
                        )
                        if not any(status.running for status in statuses):
                            break
                        await asyncio.sleep(0)
                    else:
                        self.fail("Install task did not finish.")

                self.assertEqual(len(first_manager.create_requests), 1)
                self.assertEqual(len(second_manager.create_requests), 1)

        asyncio.run(_run())

    def test_routes_use_sudo_app_manage_scope_and_preserve_recipe_inputs(self) -> None:
        app = FastAPI()
        service = _RouteService()
        register_app_installer_routes(
            app,
            service=service,
            api_prefix="/api",
            http_exception=lambda status_code, detail: HTTPException(status_code=status_code, detail=detail),
            traffic_log=logging.getLogger(__name__),
        )

        async def _request_routes() -> tuple[Response, Response, Response]:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                return (
                    await client.get("/api/app-installer"),
                    await client.post(
                        "/api/app-installer/jobs",
                        json={
                            "scope": "demo",
                            "instance_key": "alpha",
                            "friendly_name": "Demo Alpha",
                            "subfolder": "demo-alpha",
                            "steam_branch_id": "public",
                            "inputs": {"admin_password": "secret"},
                        },
                    ),
                    await client.get("/api/app-installer/jobs/job-1"),
                )

        with patch("node_api.app_installer_routes.audit_log") as audit:
            catalog_response, start_response, status_response = asyncio.run(_request_routes())

        self.assertEqual(catalog_response.status_code, 200)
        self.assertEqual(start_response.status_code, 200)
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(service.access_requests, [(None, (NodeApiScope.APP_MANAGE,))] * 3)
        self.assertEqual(service.start_requests[0][1], 42)
        self.assertEqual(service.start_requests[0][0].inputs, {AppInstallInput.ADMIN_PASSWORD: "secret"})
        audit.assert_called_once_with(
            "app.install_started",
            actor_user_id=42,
            node_name="node-a",
            app_scope="demo",
            instance_key="alpha",
            steam_branch_id="public",
            job_id="job-1",
            required_level=Power_Level.sudo.name,
        )

    def test_start_route_returns_bad_request_for_invalid_install_details(self) -> None:
        app = FastAPI()
        service = _RouteService()
        service.start_error = ValueError("That release channel is not available.")
        register_app_installer_routes(
            app,
            service=service,
            api_prefix="/api",
            http_exception=lambda status_code, detail: HTTPException(status_code=status_code, detail=detail),
            traffic_log=logging.getLogger(__name__),
        )

        async def _request_route() -> Response:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.post(
                    "/api/app-installer/jobs",
                    json={
                        "scope": "demo",
                        "instance_key": "alpha",
                        "friendly_name": "Demo Alpha",
                        "subfolder": "demo-alpha",
                        "steam_branch_id": "public",
                    },
                )

        response = asyncio.run(_request_route())

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "That release channel is not available."})
        self.assertEqual(service.start_requests, [])

    def test_start_route_returns_conflict_for_an_active_install_target(self) -> None:
        app = FastAPI()
        service = _RouteService()
        service.start_error = RuntimeError("An install is already using that folder.")
        register_app_installer_routes(
            app,
            service=service,
            api_prefix="/api",
            http_exception=lambda status_code, detail: HTTPException(status_code=status_code, detail=detail),
            traffic_log=logging.getLogger(__name__),
        )

        async def _request_route() -> Response:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.post(
                    "/api/app-installer/jobs",
                    json={
                        "scope": "demo",
                        "instance_key": "alpha",
                        "friendly_name": "Demo Alpha",
                        "subfolder": "demo-alpha",
                        "steam_branch_id": "public",
                    },
                )

        response = asyncio.run(_request_route())

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"detail": "An install is already using that folder."})
        self.assertEqual(service.start_requests, [])

    def test_node_settings_routes_use_root_node_manage_scope(self) -> None:
        app = FastAPI()
        service = _NodeSettingsRouteService()
        register_node_management_routes(
            app,
            auth=cast(Any, service),
            management=cast(Any, service),
            api_prefix="/api",
            http_exception=lambda status_code, detail: HTTPException(status_code=status_code, detail=detail),
            traffic_log=logging.getLogger(__name__),
        )

        async def _request_routes() -> tuple[Response, Response]:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                return (
                    await client.get("/api/app-installer-settings"),
                    await client.post("/api/app-installer-settings", json={"allowed_scopes": []}),
                )

        read_response, update_response = asyncio.run(_request_routes())

        self.assertEqual(read_response.status_code, 200)
        self.assertEqual(read_response.json()["settings"], {"allowed_scopes": ["demo"]})
        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(update_response.json()["settings"], {"allowed_scopes": []})
        self.assertEqual(service.access_requests, [(None, (NodeApiScope.NODE_MANAGE,))] * 2)
        self.assertEqual(
            service.mutation_requests,
            [(config.AppInstallerSettings(allowed_scopes=()), 42)],
        )


if __name__ == "__main__":
    unittest.main()

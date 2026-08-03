"""App-agnostic configuration storage operations for the node API."""

from __future__ import annotations

import logging
from collections.abc import Callable

from fastapi.responses import FileResponse

from _file import File_Utils
from _security import Access_Control
from _utils import Utilities
from apps._app import App
from apps._config_files import AppConfigFile, AppConfigFileContent, AppConfigFileRoot
from node_api_files import NodeConfigContent, NodeConfigEntry, NodeConfigList


class NodeStorageService:
    """Owns app-agnostic configuration storage operations."""

    def __init__(
        self,
        *,
        node_name: Callable[[], str],
        current_acl: Callable[[], Access_Control | None],
        invalidate_client_pack_content: Callable[[App], None],
        http_exception: Callable[[int, str], Exception],
        traffic_log: logging.Logger,
    ) -> None:
        self._node_name = node_name
        self._current_acl = current_acl
        self._invalidate_client_pack_content = invalidate_client_pack_content
        self._http_exception = http_exception
        self._traffic_log = traffic_log

    def build_config_list(
        self, *, app: App, actor_user_id: int | None = None
    ) -> NodeConfigList:
        configs = self._visible_config_files(app=app, actor_user_id=actor_user_id)
        self._traffic_log.info(
            "Node API built config list: node=%s app=%s configs=%s",
            self._node_name(),
            app.name,
            len(configs),
        )
        return NodeConfigList(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self._node_name(),
            configs=tuple(self._config_entry(config_file) for config_file in configs),
        )

    def read_config_file(self, *, app: App, config_id: str) -> NodeConfigContent:
        try:
            content = app.read_config_file(config_id)
        except FileNotFoundError as xcp:
            raise self._http_exception(404, str(xcp)) from xcp
        except ValueError as xcp:
            raise self._http_exception(400, str(xcp)) from xcp
        return self._config_content(app=app, content=content)

    def write_config_file(
        self, *, app: App, config_id: str, content: str
    ) -> NodeConfigContent:
        try:
            updated = app.write_config_file(config_id, content)
        except FileNotFoundError as xcp:
            raise self._http_exception(404, str(xcp)) from xcp
        except ValueError as xcp:
            raise self._http_exception(400, str(xcp)) from xcp
        self._traffic_log.info(
            "Node API wrote config file: node=%s app=%s config=%s",
            self._node_name(),
            app.name,
            config_id,
        )
        self._invalidate_client_pack_content(app)
        return self._config_content(app=app, content=updated)

    async def build_config_root_download_response(
        self,
        *,
        app: App,
        root_id: str,
        actor_user_id: int | None = None,
    ) -> FileResponse:
        try:
            root = app.resolve_config_root(root_id)
        except ValueError as xcp:
            raise self._http_exception(400, str(xcp)) from xcp

        acl = self._current_acl()
        if (
            actor_user_id is not None
            and acl is not None
            and not acl.can(actor_user_id, app.config_file_read_level_for_root(root_id))
        ):
            raise self._http_exception(
                403, f"Insufficient level for config root: {root.label}"
            )

        root_path = root.resolved_path
        if not root_path.exists():
            raise self._http_exception(404, f"Config root does not exist: {root.label}")
        if not root_path.is_file() and not root_path.is_dir():
            raise self._http_exception(404, f"Config root is unsupported: {root.label}")

        visible_configs = tuple(
            config_file
            for config_file in self._visible_config_files(
                app=app, actor_user_id=actor_user_id
            )
            if config_file.root_id == root_id
        )
        if not visible_configs:
            raise self._http_exception(
                404, f"No downloadable config files found in root: {root.label}"
            )
        if root_path.is_file():
            self._traffic_log.info(
                "Node API sending config file root: node=%s app=%s root=%s",
                self._node_name(),
                app.name,
                root_id,
            )
            return FileResponse(path=root_path, filename=root_path.name)

        paths = tuple(
            app.resolve_config_file(config_file.id) for config_file in visible_configs
        )
        archive_path = await File_Utils.compress(
            paths,
            self._config_root_archive_name(app=app, root=root),
            arc_base=root_path,
        )
        self._traffic_log.info(
            "Node API sending config root archive: node=%s app=%s root=%s files=%s archive=%s",
            self._node_name(),
            app.name,
            root_id,
            len(paths),
            archive_path,
        )
        return FileResponse(path=archive_path, filename=archive_path.name)

    def _visible_config_files(
        self, *, app: App, actor_user_id: int | None
    ) -> tuple[AppConfigFile, ...]:
        configs = app.list_config_files()
        acl = self._current_acl()
        if actor_user_id is None or acl is None:
            return configs
        return tuple(
            config_file
            for config_file in configs
            if acl.can(actor_user_id, config_file.read_power_level)
        )

    @staticmethod
    def _config_entry(config_file: AppConfigFile) -> NodeConfigEntry:
        return NodeConfigEntry(
            id=config_file.id,
            label=config_file.label,
            relative_path=config_file.relative_path,
            root_id=config_file.root_id,
            root_label=config_file.root_label,
            kind=config_file.kind.value,
            read_power_level=config_file.read_power_level,
            write_power_level=config_file.write_power_level,
            size_bytes=config_file.size_bytes,
            size_text=Utilities.humanise_bytes(config_file.size_bytes),
            modified_at=config_file.modified_at.isoformat(sep=" ", timespec="seconds"),
        )

    def _config_content(
        self, *, app: App, content: AppConfigFileContent
    ) -> NodeConfigContent:
        return NodeConfigContent(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self._node_name(),
            config=self._config_entry(content.file),
            content=content.content,
        )

    @staticmethod
    def _config_root_archive_name(*, app: App, root: AppConfigFileRoot) -> str:
        app_name = "".join(
            character if character.isalnum() or character in {"-", "_"} else "_"
            for character in app.friendly.strip()
        )
        root_name = "".join(
            character if character.isalnum() or character in {"-", "_", "."} else "_"
            for character in root.id.strip()
        )
        return f"{app_name.strip('_') or app.name}_{root_name.strip('_') or root.id}_configs.zip"

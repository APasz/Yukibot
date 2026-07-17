from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import config

from .client_pack_drafts import ClientPackDraftStore
from .constants import log
from .nicegui_protocols import ModWebFastApiApp, WebChatRelayPublisher
from .runtime_imports import (
    Access_Control,
    App_Manager,
    MaintenanceService,
    ManagedApp,
    ModWebAuthService,
    NodeApiService,
    NodeSystemActionHandler,
    Power_Level,
    RelayTTSQueue,
    RestartTarget,
)
from .types import (
    ModWebNotificationTrayItemKind,
    ModWebNotificationTrayItemState,
    _ModWebNotificationTrayItem,
)
from .user_settings import ModWebUserSettings, ModWebUserSettingsStore
from .utils import _http_exception

_USER_TRANSFER_LIMIT = 3
_DOWNLOAD_TRANSFER_ACTIVE_SECONDS = 8.0
_TRANSFER_DISPLAY_SECONDS = 10.0


@dataclass(slots=True)
class _TransferRecord:
    transfer_id: int
    user_id: int
    item: _ModWebNotificationTrayItem
    reserved_slots: int
    created_at: float
    active_until: float | None = None
    display_until: float | None = None

    @property
    def active(self) -> bool:
        return self.display_until is None


class ModWebDashboardBackend:
    def __init__(
        self,
        *,
        auth: ModWebAuthService | None = None,
        node_api: NodeApiService | None = None,
        client_pack_drafts: ClientPackDraftStore | None = None,
        user_settings: ModWebUserSettingsStore | None = None,
    ) -> None:
        self._manager: App_Manager | None = None
        self._acl: Access_Control | None = None
        self._auth: ModWebAuthService = auth or ModWebAuthService()
        self._node_api: NodeApiService = node_api or NodeApiService()
        session_cache_directory: Path | None = config.MOD_WEB_AUTH.session_cache_directory
        draft_cache_directory = (
            None
            if session_cache_directory is None
            else session_cache_directory / "client_pack_drafts"
        )
        self._client_pack_drafts = client_pack_drafts or ClientPackDraftStore(
            draft_cache_directory
        )
        self._user_settings = user_settings or ModWebUserSettingsStore()
        self._chat_relay: WebChatRelayPublisher | None = None
        self._transfer_lock = threading.Lock()
        self._transfer_records: dict[int, _TransferRecord] = {}
        self._transfer_auto_complete_timers: dict[int, threading.Timer] = {}
        self._transfer_expiry_timers: dict[int, threading.Timer] = {}
        self._transfer_subscribers: dict[int, dict[int, Callable[[], None]]] = {}
        self._next_transfer_id = 1
        self._next_transfer_subscription_id = 1
        self._node_api.set_web_auth(self._auth)

    @property
    def manager(self) -> App_Manager | None:
        return self._manager

    @property
    def acl(self) -> Access_Control | None:
        return self._acl

    @property
    def auth(self) -> ModWebAuthService:
        return self._auth

    @property
    def node_api(self) -> NodeApiService:
        return self._node_api

    @property
    def chat_relay(self) -> WebChatRelayPublisher | None:
        return self._chat_relay

    def user_settings_for(self, *, user_id: int) -> ModWebUserSettings:
        return self._user_settings.get(user_id=user_id)

    def save_user_settings(self, *, user_id: int, settings: ModWebUserSettings) -> bool:
        return self._user_settings.set(user_id=user_id, settings=settings)

    def set_manager(self, manager: App_Manager) -> None:
        self._manager = manager
        self._node_api.set_manager(manager)

    def replace_manager(self, manager: App_Manager | None) -> None:
        self._manager = manager
        if manager is not None:
            self._node_api.set_manager(manager)

    def set_acl(self, acl: Access_Control) -> None:
        self._acl = acl
        self._node_api.set_acl(acl)

    def replace_acl(self, acl: Access_Control | None) -> None:
        self._acl = acl
        if acl is not None:
            self._node_api.set_acl(acl)

    def set_relay_tts_service(self, relay_tts_service: RelayTTSQueue | None) -> None:
        self._node_api.set_relay_tts_service(relay_tts_service)

    def set_chat_relay_service(self, chat_relay: WebChatRelayPublisher | None) -> None:
        self._chat_relay = chat_relay
        self._node_api.set_chat_relay_service(chat_relay)

    def set_system_action_handler(self, handler: NodeSystemActionHandler) -> None:
        self._node_api.set_system_action_handler(handler)

    def set_maintenance_service(
        self,
        maintenance_service: MaintenanceService,
        available_targets: tuple[RestartTarget, ...],
    ) -> None:
        self._node_api.set_maintenance_service(maintenance_service, available_targets)

    def replace_chat_relay_service(self, chat_relay: WebChatRelayPublisher | None) -> None:
        self.set_chat_relay_service(chat_relay)

    def register_node_api_routes(self, nicegui_app: ModWebFastApiApp) -> None:
        self._node_api.register_routes(nicegui_app)

    def start_background_tasks(self) -> None:
        self._node_api.start_background_tasks()

    def begin_shutdown(self) -> None:
        self._node_api.begin_shutdown()
        self._auth.close()
        self._client_pack_drafts.close()

    def client_pack_changelog_draft(self, *, node_name: str, app_name: str) -> str | None:
        return self._client_pack_drafts.get(node_name=node_name, app_name=app_name)

    def set_client_pack_changelog_draft(
        self,
        *,
        node_name: str,
        app_name: str,
        changelog: str,
    ) -> None:
        self._client_pack_drafts.set(
            node_name=node_name,
            app_name=app_name,
            changelog=changelog,
        )

    def clear_client_pack_changelog_draft(self, *, node_name: str, app_name: str) -> None:
        self._client_pack_drafts.clear(node_name=node_name, app_name=app_name)

    def resolve_app(self, app_name: str) -> ManagedApp:
        if self._manager is None:
            log.warning("Mod web app resolve failed because manager is missing: app=%s", app_name)
            raise _http_exception(503, "App manager is not available yet.")
        try:
            log.debug("Mod web resolving app: app=%s", app_name)
            return self._manager.get(app_name)
        except Exception as xcp:
            log.warning("Mod web app not found: app=%s", app_name)
            raise _http_exception(404, f"Unknown app: {app_name}") from xcp

    def managed_apps(self) -> tuple[ManagedApp, ...]:
        if self._manager is None:
            return ()
        return tuple(sorted(self._manager.apps.values(), key=lambda item: item.friendly.casefold()))

    def user_has_level(self, *, user_id: int, required_level: Power_Level) -> bool:
        if self._acl is None:
            return False
        return self._acl.can(user_id, required_level)

    def user_level(self, *, user_id: int) -> Power_Level:
        if self._acl is None:
            return Power_Level.guest
        return self._acl.level_of(user_id)

    def start_transfers(
        self,
        *,
        user_id: int,
        kind: ModWebNotificationTrayItemKind,
        filenames: tuple[str, ...],
        detail_text: str,
        initial_progress_percent: float,
        node_color_hex: str | None = None,
        app_color_hex: str | None = None,
        auto_complete_after_seconds: float | None = None,
    ) -> tuple[int, ...]:
        if not filenames:
            raise ValueError("At least one transfer filename is required.")
        now = time.monotonic()
        scheduled_auto_complete_ids: list[int] = []
        with self._transfer_lock:
            self._cleanup_transfer_records(now=now)
            required_slots: int = len(filenames)
            if self._active_transfer_slots_locked(user_id=user_id) + required_slots > _USER_TRANSFER_LIMIT:
                raise RuntimeError(
                    f"Transfer limit reached. You can run at most {_USER_TRANSFER_LIMIT} uploads/downloads at once."
                )
            transfer_ids: list[int] = []
            for filename in filenames:
                transfer_id = self._next_transfer_id
                self._next_transfer_id += 1
                self._transfer_records[transfer_id] = _TransferRecord(
                    transfer_id=transfer_id,
                    user_id=user_id,
                    item=_ModWebNotificationTrayItem(
                        kind=kind,
                        state=ModWebNotificationTrayItemState.ACTIVE,
                        label=filename,
                        detail_text=detail_text,
                        progress_percent=initial_progress_percent,
                        node_color_hex=node_color_hex,
                        app_color_hex=app_color_hex,
                        blink=True,
                    ),
                    reserved_slots=1,
                    created_at=now,
                    active_until=None if auto_complete_after_seconds is None else now + auto_complete_after_seconds,
                )
                if auto_complete_after_seconds is not None:
                    scheduled_auto_complete_ids.append(transfer_id)
                transfer_ids.append(transfer_id)
        if auto_complete_after_seconds is not None:
            for transfer_id in scheduled_auto_complete_ids:
                self._schedule_transfer_auto_complete(
                    transfer_id=transfer_id,
                    delay_seconds=auto_complete_after_seconds,
                )
        self._notify_transfer_subscribers(user_id=user_id)
        return tuple(transfer_ids)

    def start_download_transfers(
        self,
        *,
        user_id: int,
        filenames: tuple[str, ...],
        detail_text: str,
        node_color_hex: str | None = None,
        app_color_hex: str | None = None,
    ) -> tuple[int, ...]:
        return self.start_transfers(
            user_id=user_id,
            kind=ModWebNotificationTrayItemKind.DOWNLOAD,
            filenames=filenames,
            detail_text=detail_text,
            initial_progress_percent=4.0,
            node_color_hex=node_color_hex,
            app_color_hex=app_color_hex,
            auto_complete_after_seconds=_DOWNLOAD_TRANSFER_ACTIVE_SECONDS,
        )

    def start_upload_transfers(
        self,
        *,
        user_id: int,
        filenames: tuple[str, ...],
        detail_text: str,
        node_color_hex: str | None = None,
        app_color_hex: str | None = None,
    ) -> tuple[int, ...]:
        return self.start_transfers(
            user_id=user_id,
            kind=ModWebNotificationTrayItemKind.UPLOAD,
            filenames=filenames,
            detail_text=detail_text,
            initial_progress_percent=6.0,
            node_color_hex=node_color_hex,
            app_color_hex=app_color_hex,
            auto_complete_after_seconds=None,
        )

    def start_simulated_transfer(
        self,
        *,
        user_id: int,
        kind: ModWebNotificationTrayItemKind,
        filename: str,
        detail_text: str,
        duration_seconds: float,
        node_color_hex: str | None = None,
        app_color_hex: str | None = None,
    ) -> int:
        initial_progress_percent: float = 4.0 if kind is ModWebNotificationTrayItemKind.DOWNLOAD else 8.0
        transfer_ids = self.start_transfers(
            user_id=user_id,
            kind=kind,
            filenames=(filename,),
            detail_text=detail_text,
            initial_progress_percent=initial_progress_percent,
            node_color_hex=node_color_hex,
            app_color_hex=app_color_hex,
            auto_complete_after_seconds=duration_seconds,
        )
        return transfer_ids[0]

    def complete_transfer(self, *, transfer_id: int, detail_text: str | None = None) -> None:
        self._finish_transfer(
            transfer_id=transfer_id,
            state=ModWebNotificationTrayItemState.SUCCESS,
            detail_text=detail_text,
            progress_percent=100.0,
        )

    def fail_transfer(self, *, transfer_id: int, detail_text: str) -> None:
        self._finish_transfer(
            transfer_id=transfer_id,
            state=ModWebNotificationTrayItemState.ERROR,
            detail_text=detail_text,
            progress_percent=100.0,
        )

    def update_transfer_progress(
        self,
        *,
        transfer_id: int,
        progress_percent: float,
        detail_text: str | None = None,
        blink: bool | None = None,
    ) -> None:
        now = time.monotonic()
        target_user_id: int | None = None
        with self._transfer_lock:
            self._cleanup_transfer_records(now=now)
            record = self._transfer_records.get(transfer_id)
            if record is None or not record.active:
                return
            target_user_id = record.user_id
            record.item = replace(
                record.item,
                detail_text=record.item.detail_text if detail_text is None else detail_text,
                progress_percent=progress_percent,
                blink=record.item.blink if blink is None else blink,
            )
        if target_user_id is not None:
            self._notify_transfer_subscribers(user_id=target_user_id)

    def user_transfer_items(self, *, user_id: int) -> tuple[_ModWebNotificationTrayItem, ...]:
        now = time.monotonic()
        with self._transfer_lock:
            self._cleanup_transfer_records(now=now)
            records = tuple(record for record in self._transfer_records.values() if record.user_id == user_id)
        ordered_records = sorted(records, key=lambda record: (-int(record.active), -record.created_at, -record.transfer_id))
        return tuple(self._record_item(record, now=now) for record in ordered_records)

    def user_active_transfer_slots(self, *, user_id: int) -> int:
        now = time.monotonic()
        with self._transfer_lock:
            self._cleanup_transfer_records(now=now)
            return self._active_transfer_slots_locked(user_id=user_id)

    def clear_user_transfers(self, *, user_id: int) -> None:
        with self._transfer_lock:
            transfer_ids = tuple(
                transfer_id
                for transfer_id, record in self._transfer_records.items()
                if record.user_id == user_id
            )
            for transfer_id in transfer_ids:
                self._cancel_transfer_timers_locked(transfer_id=transfer_id)
                self._transfer_records.pop(transfer_id, None)
        if transfer_ids:
            self._notify_transfer_subscribers(user_id=user_id)

    @staticmethod
    def transfer_limit() -> int:
        return _USER_TRANSFER_LIMIT

    def subscribe_user_transfers(self, *, user_id: int, subscriber: Callable[[], None]) -> Callable[[], None]:
        with self._transfer_lock:
            subscription_id = self._next_transfer_subscription_id
            self._next_transfer_subscription_id += 1
            self._transfer_subscribers.setdefault(user_id, {})[subscription_id] = subscriber

        def _unsubscribe() -> None:
            with self._transfer_lock:
                subscribers = self._transfer_subscribers.get(user_id)
                if subscribers is None:
                    return
                subscribers.pop(subscription_id, None)
                if not subscribers:
                    self._transfer_subscribers.pop(user_id, None)

        return _unsubscribe

    def _finish_transfer(
        self,
        *,
        transfer_id: int,
        state: ModWebNotificationTrayItemState,
        detail_text: str | None,
        progress_percent: float,
    ) -> None:
        now = time.monotonic()
        target_user_id: int | None = None
        with self._transfer_lock:
            self._cleanup_transfer_records(now=now)
            record = self._transfer_records.get(transfer_id)
            if record is None:
                return
            target_user_id = record.user_id
            record.item = replace(
                record.item,
                state=state,
                detail_text=record.item.detail_text if detail_text is None else detail_text,
                progress_percent=progress_percent,
                blink=False,
            )
            record.active_until = None
            record.display_until = now + _TRANSFER_DISPLAY_SECONDS
            self._cancel_transfer_auto_complete_timer_locked(transfer_id=transfer_id)
            self._schedule_transfer_expiry_locked(transfer_id=transfer_id, delay_seconds=_TRANSFER_DISPLAY_SECONDS)
        if target_user_id is not None:
            self._notify_transfer_subscribers(user_id=target_user_id)

    def _cleanup_transfer_records(self, *, now: float) -> None:
        expired_transfer_ids: list[int] = []
        for transfer_id, record in self._transfer_records.items():
            if record.display_until is not None and now >= record.display_until:
                expired_transfer_ids.append(transfer_id)
        for transfer_id in expired_transfer_ids:
            self._cancel_transfer_timers_locked(transfer_id=transfer_id)
            self._transfer_records.pop(transfer_id, None)

    def _active_transfer_slots_locked(self, *, user_id: int) -> int:
        return sum(
            record.reserved_slots
            for record in self._transfer_records.values()
            if record.user_id == user_id and record.active
        )

    @staticmethod
    def _record_item(record: _TransferRecord, *, now: float) -> _ModWebNotificationTrayItem:
        if record.item.state is not ModWebNotificationTrayItemState.ACTIVE or record.active_until is None:
            return record.item
        duration_seconds: float = max(record.active_until - record.created_at, 0.001)
        elapsed_seconds: float = max(0.0, min(now - record.created_at, duration_seconds))
        progress_percent: float = min(
            92.0,
            max(record.item.progress_percent or 0.0, (elapsed_seconds / duration_seconds) * 92.0),
        )
        return replace(record.item, progress_percent=progress_percent)

    def _auto_complete_transfer(self, *, transfer_id: int) -> None:
        self.complete_transfer(transfer_id=transfer_id, detail_text="Browser-managed download started.")

    def _expire_transfer(self, *, transfer_id: int) -> None:
        target_user_id: int | None = None
        with self._transfer_lock:
            record = self._transfer_records.get(transfer_id)
            if record is None:
                return
            target_user_id = record.user_id
            self._cancel_transfer_timers_locked(transfer_id=transfer_id)
            self._transfer_records.pop(transfer_id, None)
        if target_user_id is not None:
            self._notify_transfer_subscribers(user_id=target_user_id)

    def _schedule_transfer_auto_complete(self, *, transfer_id: int, delay_seconds: float) -> None:
        with self._transfer_lock:
            self._schedule_transfer_auto_complete_locked(transfer_id=transfer_id, delay_seconds=delay_seconds)

    def _schedule_transfer_auto_complete_locked(self, *, transfer_id: int, delay_seconds: float) -> None:
        self._cancel_transfer_auto_complete_timer_locked(transfer_id=transfer_id)
        timer = threading.Timer(delay_seconds, lambda: self._auto_complete_transfer(transfer_id=transfer_id))
        timer.daemon = True
        self._transfer_auto_complete_timers[transfer_id] = timer
        timer.start()

    def _schedule_transfer_expiry_locked(self, *, transfer_id: int, delay_seconds: float) -> None:
        self._cancel_transfer_expiry_timer_locked(transfer_id=transfer_id)
        timer = threading.Timer(delay_seconds, lambda: self._expire_transfer(transfer_id=transfer_id))
        timer.daemon = True
        self._transfer_expiry_timers[transfer_id] = timer
        timer.start()

    def _cancel_transfer_timers_locked(self, *, transfer_id: int) -> None:
        self._cancel_transfer_auto_complete_timer_locked(transfer_id=transfer_id)
        self._cancel_transfer_expiry_timer_locked(transfer_id=transfer_id)

    def _cancel_transfer_auto_complete_timer_locked(self, *, transfer_id: int) -> None:
        timer = self._transfer_auto_complete_timers.pop(transfer_id, None)
        if timer is not None:
            timer.cancel()

    def _cancel_transfer_expiry_timer_locked(self, *, transfer_id: int) -> None:
        timer = self._transfer_expiry_timers.pop(transfer_id, None)
        if timer is not None:
            timer.cancel()

    def _notify_transfer_subscribers(self, *, user_id: int) -> None:
        with self._transfer_lock:
            subscribers = tuple(self._transfer_subscribers.get(user_id, {}).values())
        for subscriber in subscribers:
            try:
                subscriber()
            except Exception:
                log.exception("Transfer subscriber callback failed: user_id=%s", user_id)


__all__: tuple[str, ...] = ("ModWebDashboardBackend",)

"""System telemetry, history, and log browsing for the node API."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import psutil

import config
from _manager import App_Manager, app_scope_from_name
from _sys import Stats_System, StatsDiskSnapshot, StatsSystemSnapshot
from deployment_metadata import DeploymentMetadata
from node_api_system import (
    NodeSystemDiskSummary,
    NodeSystemHistory,
    NodeSystemLogCatalog,
    NodeSystemLogEntry,
    NodeSystemLogTail,
    NodeSystemSample,
    NodeSystemSummary,
)


@dataclass(frozen=True, slots=True)
class _TimedSystemSummary:
    captured_at_seconds: float
    summary: NodeSystemSummary


class NodeSystemMonitoringService:
    """Owns cached system observations and the system-log catalogue."""

    def __init__(
        self,
        *,
        node_name: Callable[[], str],
        manager: Callable[[], App_Manager | None],
        stats_factory: Callable[[], Stats_System],
        http_exception: Callable[[int, str], Exception],
        logger: logging.Logger,
        summary_cache_ttl_seconds: float,
        history_retention_seconds: int,
        history_interval_seconds: float,
        max_log_lines: int,
    ) -> None:
        if summary_cache_ttl_seconds <= 0:
            raise ValueError("System summary cache TTL must be positive.")
        if history_retention_seconds <= 0:
            raise ValueError("System history retention must be positive.")
        if history_interval_seconds <= 0:
            raise ValueError("System history interval must be positive.")
        if max_log_lines <= 0:
            raise ValueError("System log line limit must be positive.")
        self._node_name = node_name
        self._manager = manager
        self._stats_factory = stats_factory
        self._http_exception = http_exception
        self._log = logger
        self._summary_cache_ttl_seconds = summary_cache_ttl_seconds
        self._history_retention_seconds = history_retention_seconds
        self._history_interval_seconds = history_interval_seconds
        self._max_log_lines = max_log_lines
        self._summary_cache: _TimedSystemSummary | None = None
        self._summary_cache_lock = threading.RLock()
        self._history: deque[NodeSystemSample] = deque(
            maxlen=max(1, int(history_retention_seconds // history_interval_seconds))
        )
        self._history_lock = threading.RLock()

    def invalidate_summary_cache(self) -> None:
        with self._summary_cache_lock:
            self._summary_cache = None

    def build_summary(self, *, force_refresh: bool = False) -> NodeSystemSummary:
        with self._summary_cache_lock:
            now = time.monotonic()
            cached = self._summary_cache
            if (
                not force_refresh
                and cached is not None
                and now - cached.captured_at_seconds < self._summary_cache_ttl_seconds
            ):
                return cached.summary
            summary = self._build_summary_uncached()
            self._record_sample(summary)
            self._summary_cache = _TimedSystemSummary(
                captured_at_seconds=time.monotonic(),
                summary=summary,
            )
            return summary

    def build_history(self) -> NodeSystemHistory:
        with self._history_lock:
            samples = tuple(self._history)
        return NodeSystemHistory(
            retention_seconds=self._history_retention_seconds,
            sample_interval_seconds=int(self._history_interval_seconds),
            samples=samples,
        )

    def build_log_catalog(self) -> NodeSystemLogCatalog:
        return NodeSystemLogCatalog(
            node=self._node_name(),
            entries=tuple(entry for entry, _path in self._log_entries_with_paths()),
        )

    def build_log_tail(
        self, *, log_path: str, max_lines: int = 200
    ) -> NodeSystemLogTail:
        if max_lines < 1 or max_lines > self._max_log_lines:
            raise ValueError(
                f"System log line limit must be between 1 and {self._max_log_lines}."
            )
        path_by_relative_path = {
            entry.relative_path: (entry, path)
            for entry, path in self._log_entries_with_paths()
        }
        resolved = path_by_relative_path.get(log_path)
        if resolved is None:
            raise self._http_exception(404, "Unknown system log.")
        entry, path = resolved
        try:
            lines, truncated = self._read_log_tail(path=path, max_lines=max_lines)
        except OSError as xcp:
            raise self._http_exception(500, f"System log read failed: {xcp}") from xcp
        return NodeSystemLogTail(
            node=self._node_name(),
            entry=entry,
            lines=lines,
            truncated=truncated,
        )

    def _record_sample(self, summary: NodeSystemSummary) -> None:
        sample = NodeSystemSample.from_summary(summary)
        with self._history_lock:
            if self._history:
                previous = self._history[-1]
                elapsed = (
                    sample.captured_at_epoch_seconds
                    - previous.captured_at_epoch_seconds
                )
                if elapsed < 0:
                    self._history.clear()
                elif elapsed < self._history_interval_seconds:
                    return
            self._history.append(sample)

    @staticmethod
    def _read_log_tail(*, path: Path, max_lines: int) -> tuple[tuple[str, ...], bool]:
        if max_lines < 1:
            raise ValueError("Log tail line limit must be at least 1.")
        with path.open("rb") as handle:
            handle.seek(0, 2)
            file_size = handle.tell()
            if file_size <= 0:
                return (), False

            chunk_size = 8192
            position = file_size
            buffer = bytearray()
            newline_count = 0
            while position > 0 and newline_count <= max_lines:
                read_size = min(chunk_size, position)
                position -= read_size
                handle.seek(position)
                chunk = handle.read(read_size)
                if not chunk:
                    break
                buffer[:0] = chunk
                newline_count = buffer.count(b"\n")

        lines = tuple(
            deque(
                buffer.decode(config.STR_ENCODE, errors="replace").splitlines(),
                maxlen=max_lines,
            )
        )
        return lines, position > 0 or newline_count > max_lines

    def _log_entries_with_paths(self) -> tuple[tuple[NodeSystemLogEntry, Path], ...]:
        log_root = config.DIR_LOG.resolve(strict=False)
        if not log_root.exists():
            return ()
        if not log_root.is_dir():
            raise RuntimeError(f"System log directory is not a directory: {log_root}")

        candidates = sorted(
            (candidate for candidate in log_root.rglob("*") if candidate.is_file()),
            key=lambda candidate: (
                len(candidate.relative_to(log_root).parts),
                candidate.as_posix().casefold(),
            ),
        )
        entries_by_target: dict[Path, tuple[NodeSystemLogEntry, Path]] = {}
        for candidate in candidates:
            relative_path = candidate.relative_to(log_root).as_posix()
            try:
                target_path = candidate.resolve(strict=True)
                if not target_path.is_relative_to(log_root):
                    self._log.warning(
                        "Skipping system log outside log directory: path=%s", candidate
                    )
                    continue
                stat = target_path.stat()
            except OSError as xcp:
                self._log.warning(
                    "Skipping unreadable system log: path=%s error=%s", candidate, xcp
                )
                continue
            entries_by_target.setdefault(
                target_path,
                (
                    NodeSystemLogEntry(
                        relative_path=relative_path,
                        size_bytes=stat.st_size,
                        modified_at_epoch_seconds=max(0, int(stat.st_mtime)),
                    ),
                    target_path,
                ),
            )
        return tuple(
            sorted(
                entries_by_target.values(),
                key=lambda item: item[0].relative_path.casefold(),
            )
        )

    def _build_summary_uncached(self) -> NodeSystemSummary:
        cpu_percent: int | None = None
        cpu_per_core_percent: tuple[int, ...] = ()
        ram_percent: int | None = None
        ram_used_bytes: int | None = None
        ram_total_bytes: int | None = None
        storage_percent: int | None = None
        storage_free_bytes: int | None = None
        storage_total_bytes: int | None = None
        disks: tuple[NodeSystemDiskSummary, ...] = ()
        bot_uptime_seconds: int | None = None
        uptime_seconds: int | None = None
        cpu_points_available: int | None = None
        cpu_points_capacity: int | None = None
        ram_points_available: int | None = None
        ram_points_capacity: int | None = None
        running_names: tuple[str, ...] = ()
        running_app_ids: tuple[str, ...] = ()
        running_app_scopes: tuple[str, ...] = ()
        start_blocked_app_ids: tuple[str, ...] = ()
        deployment_metadata: DeploymentMetadata | None = (
            config.MOD_WEB_DEPLOYMENT_METADATA
        )
        deployment_version = (
            deployment_metadata.version
            if deployment_metadata is not None
            else "indev"
            if config.INDEV
            else None
        )
        deployment_revision = (
            config.MOD_WEB_BUILD_SHA
            if deployment_metadata is None
            else deployment_metadata.revision
        )
        deployed_at_epoch_seconds = (
            None
            if deployment_metadata is None
            else int(deployment_metadata.deployed_at.timestamp())
        )

        try:
            system_stats = self._stats_factory()
            snapshot: StatsSystemSnapshot = system_stats.system_snapshot(refresh=True)
        except Exception as xcp:
            self._log.warning(
                "Node API system stats failed: node=%s error=%s", self._node_name(), xcp
            )
        else:
            cpu_percent = snapshot.cpu_percent
            cpu_per_core_percent = snapshot.cpu_per_core_percent
            ram_percent = snapshot.ram_percent
            ram_used_bytes = snapshot.ram_used_bytes
            ram_total_bytes = snapshot.ram_total_bytes
            primary_disk: StatsDiskSnapshot | None = snapshot.primary_disk
            if primary_disk is not None:
                storage_percent = primary_disk.percent
                storage_free_bytes = primary_disk.free_bytes
                storage_total_bytes = primary_disk.total_bytes
            disks = tuple(
                NodeSystemDiskSummary(
                    mountpoint=disk.mountpoint_text,
                    label=disk.display_name,
                    percent=disk.percent,
                    free_bytes=disk.free_bytes,
                    total_bytes=disk.total_bytes,
                )
                for disk in snapshot.disks
            )
        try:
            bot_uptime_seconds = max(
                0, int(time.time() - psutil.Process().create_time())
            )
        except Exception as xcp:
            self._log.warning(
                "Node API bot uptime probe failed: node=%s error=%s",
                self._node_name(),
                xcp,
            )
        try:
            uptime_seconds = max(0, int(time.time() - psutil.boot_time()))
        except Exception as xcp:
            self._log.warning(
                "Node API uptime probe failed: node=%s error=%s", self._node_name(), xcp
            )

        manager = self._manager()
        if manager is not None:
            try:
                capacity = manager.node_capacity()
                usage = manager.active_resource_point_usage()
            except Exception as xcp:
                self._log.warning(
                    "Node API resource point summary failed: node=%s error=%s",
                    self._node_name(),
                    xcp,
                )
            else:
                cpu_points_capacity = capacity.cpu_points_available
                ram_points_capacity = capacity.ram_points_available
                cpu_points_available = max(0, cpu_points_capacity - usage.cpu_points)
                ram_points_available = max(0, ram_points_capacity - usage.ram_points)
            running_apps = tuple(
                (
                    app.name,
                    app.friendly,
                    app.scope
                    if isinstance(getattr(app, "scope", None), str)
                    else app_scope_from_name(app.name) or "",
                )
                for app in sorted(
                    manager.apps.values(), key=lambda item: item.friendly.casefold()
                )
                if app.check_running()
            )
            running_names = tuple(
                app_friendly for _app_name, app_friendly, _app_scope in running_apps
            )
            running_app_ids = tuple(
                app_name for app_name, _app_friendly, _app_scope in running_apps
            )
            running_app_scopes = tuple(
                app_scope for _app_name, _app_friendly, app_scope in running_apps
            )
            start_blocked_app_ids = tuple(
                app.name
                for app in sorted(
                    manager.apps.values(), key=lambda item: item.friendly.casefold()
                )
                if not app.check_running()
                and manager.start_blocker(app, include_current_activity=False)
                is not None
            )

        return NodeSystemSummary(
            cpu_percent=cpu_percent,
            ram_percent=ram_percent,
            ram_used_bytes=ram_used_bytes,
            ram_total_bytes=ram_total_bytes,
            storage_percent=storage_percent,
            storage_free_bytes=storage_free_bytes,
            storage_total_bytes=storage_total_bytes,
            cpu_per_core_percent=cpu_per_core_percent,
            disks=disks,
            bot_uptime_seconds=bot_uptime_seconds,
            uptime_seconds=uptime_seconds,
            cpu_points_available=cpu_points_available,
            cpu_points_capacity=cpu_points_capacity,
            ram_points_available=ram_points_available,
            ram_points_capacity=ram_points_capacity,
            running_names=running_names,
            running_app_ids=running_app_ids,
            running_app_scopes=running_app_scopes,
            start_blocked_app_ids=start_blocked_app_ids,
            captured_at_epoch_seconds=int(time.time()),
            deployment_version=deployment_version,
            deployment_revision=deployment_revision,
            deployed_at_epoch_seconds=deployed_at_epoch_seconds,
        )

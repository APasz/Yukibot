import asyncio
import json
import logging
import subprocess
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import hikari
import lightbulb
import psutil

import config
from _manager import App_Manager
from config import Singleton
from restart_state import mark_pending_process_restart, process_restart_kind

log = logging.getLogger(__name__)
_IGNORED_SYSTEM_MOUNTPOINTS = frozenset({"/boot", "/boot/efi", "/efi"})
_DISK_DISCOVERY_REFRESH_INTERVAL = timedelta(minutes=1)
class Stats_CPU:
    def __init__(self):
        self.last_updated: datetime | None = None
        self.total: float = 0.0
        self.per_core: list[float] = []

    def update(self):
        self.total = psutil.cpu_percent(interval=None)
        self.per_core = psutil.cpu_percent(interval=None, percpu=True)
        self.last_updated = datetime.now()

    @property
    def r_total(self) -> int:
        return round(self.total)

    @property
    def r_per_core(self) -> list[int]:
        return [round(c) for c in self.per_core]


class Stats_RAM:
    def __init__(self):
        self.last_updated: datetime | None = None
        self.raw = psutil.virtual_memory()
        self.swap = psutil.swap_memory()

    def update(self):
        self.raw = psutil.virtual_memory()
        self.swap = psutil.swap_memory()
        self.last_updated = datetime.now()

    @property
    def used(self) -> int:
        return self.raw.used

    @property
    def percent(self) -> int:
        return round(self.raw.percent)

    @property
    def swap_percent(self) -> int:
        return round(self.swap.percent)


@dataclass(frozen=True, slots=True)
class BlockDeviceMetadata:
    device: str
    filesystem: str | None
    mountpoint: str | None
    partition_label: str | None
    type_name: str | None
    volume_label: str | None


@dataclass(frozen=True, slots=True)
class DiskDescriptor:
    mountpoint: Path
    configured_label: str | None
    device: str
    filesystem: str
    options: tuple[str, ...]
    partition_label: str | None
    type_name: str
    volume_label: str | None

    @property
    def mountpoint_text(self) -> str:
        return str(self.mountpoint)


class Stats_Disk:
    def __init__(self, descriptor: DiskDescriptor):
        self._descriptor = descriptor
        self.last_updated: datetime | None = None
        self.usage = psutil.disk_usage(self.mountpoint_text)
        self.last_updated = datetime.now()

    @property
    def descriptor(self) -> DiskDescriptor:
        return self._descriptor

    @property
    def mountpoint(self) -> Path:
        return self._descriptor.mountpoint

    @property
    def mountpoint_text(self) -> str:
        return self._descriptor.mountpoint_text

    @property
    def path(self) -> Path:
        return self.mountpoint

    @property
    def device(self) -> str:
        return self._descriptor.device

    @property
    def filesystem(self) -> str:
        return self._descriptor.filesystem

    @property
    def options(self) -> tuple[str, ...]:
        return self._descriptor.options

    @property
    def configured_label(self) -> str | None:
        return self._descriptor.configured_label

    @property
    def volume_label(self) -> str | None:
        return self._descriptor.volume_label

    @property
    def partition_label(self) -> str | None:
        return self._descriptor.partition_label

    @property
    def type_name(self) -> str:
        return self._descriptor.type_name

    @property
    def label(self) -> str | None:
        if self.configured_label is not None:
            return self.configured_label
        if self.volume_label is not None:
            return self.volume_label
        return self.partition_label

    @property
    def label_source(self) -> Literal["configured", "volume", "partition", "none"]:
        if self.configured_label is not None:
            return "configured"
        if self.volume_label is not None:
            return "volume"
        if self.partition_label is not None:
            return "partition"
        return "none"

    @property
    def display_name(self) -> str:
        label = self.label
        if label is not None:
            return label
        return self.mountpoint_text

    def replace_descriptor(self, descriptor: DiskDescriptor) -> None:
        self._descriptor = descriptor

    def update(self):
        self.usage = psutil.disk_usage(self.mountpoint_text)
        self.last_updated = datetime.now()

    @property
    def percent(self) -> int:
        return round(self.usage.percent)


@dataclass(frozen=True, slots=True)
class StatsDiskSnapshot:
    mountpoint_text: str
    display_name: str
    percent: int
    free_bytes: int
    total_bytes: int


@dataclass(frozen=True, slots=True)
class StatsSystemSnapshot:
    cpu_percent: int
    cpu_per_core_percent: tuple[int, ...]
    ram_percent: int
    ram_used_bytes: int
    ram_total_bytes: int
    primary_disk: StatsDiskSnapshot | None
    disks: tuple[StatsDiskSnapshot, ...]


class Stats_System(metaclass=Singleton):
    _BOT_CONFIGURATION_PATH = Path("configuration.json")

    def __init__(self):
        self._lock = threading.RLock()
        self.cpu = Stats_CPU()
        self.ram = Stats_RAM()
        self._bot_path = Path.cwd().resolve()
        self._bot_configuration_path = self._BOT_CONFIGURATION_PATH
        self._configured_activity_mounts: tuple[str, ...] | None = None
        self._configured_labels: dict[str, str] = {}
        self._configured_primary_mount: str | None = None
        self._configured_secondary_mount: str | None = None
        self._disks_by_mountpoint: dict[str, Stats_Disk] = {}
        self._last_disk_discovery_at: datetime | None = None
        self.reload_disk_preferences()
        self.refresh_disk_inventory()

    @property
    def disks(self) -> tuple[Stats_Disk, ...]:
        with self._lock:
            return tuple(self._disks_by_mountpoint[mountpoint] for mountpoint in sorted(self._disks_by_mountpoint))

    @property
    def configured_activity_mounts(self) -> tuple[str, ...] | None:
        return self._configured_activity_mounts

    @property
    def configured_primary_mount(self) -> str | None:
        return self._configured_primary_mount

    @property
    def configured_secondary_mount(self) -> str | None:
        return self._configured_secondary_mount

    @property
    def configured_labels(self) -> Mapping[str, str]:
        return self._configured_labels

    @property
    def disk_preferences(self) -> config.PersistedDiskPreferences:
        with self._lock:
            return self._current_disk_preferences_unlocked()

    @property
    def bot_disk(self) -> Stats_Disk | None:
        with self._lock:
            return self.disk_for_path(self._bot_path)

    @property
    def primary_disk_source(self) -> Literal["override", "bot_path", "fallback"]:
        with self._lock:
            if self._configured_primary_mount is not None and self._configured_primary_mount in self._disks_by_mountpoint:
                return "override"
            if self.bot_disk is not None:
                return "bot_path"
            return "fallback"

    @property
    def primary_disk(self) -> Stats_Disk | None:
        with self._lock:
            if self._configured_primary_mount is not None:
                disk = self._disks_by_mountpoint.get(self._configured_primary_mount)
                if disk is not None:
                    return disk
            bot_disk = self.bot_disk
            if bot_disk is not None:
                return bot_disk
            disks = self.disks
            return disks[0] if disks else None

    @property
    def secondary_disk(self) -> Stats_Disk | None:
        with self._lock:
            if self._configured_secondary_mount is None:
                return None
            return self._disks_by_mountpoint.get(self._configured_secondary_mount)

    @property
    def disk(self) -> Stats_Disk:
        with self._lock:
            primary_disk = self.primary_disk
            if primary_disk is None:
                raise RuntimeError("No disks discovered.")
            return primary_disk

    @property
    def activity_disks(self) -> tuple[Stats_Disk, ...]:
        with self._lock:
            if self._configured_activity_mounts is None:
                return self.disks
            return tuple(
                self._disks_by_mountpoint[mountpoint]
                for mountpoint in self._configured_activity_mounts
                if mountpoint in self._disks_by_mountpoint
            )

    def disk_for_path(self, path: Path) -> Stats_Disk | None:
        with self._lock:
            resolved = path.resolve(strict=False)
            matches = [disk for disk in self.disks if self._path_is_within(resolved, disk.mountpoint)]
            if not matches:
                return None
            return max(matches, key=lambda disk: len(disk.mountpoint.parts))

    def reload_disk_preferences(self) -> bool:
        with self._lock:
            return self._reload_disk_preferences_unlocked()

    def _reload_disk_preferences_unlocked(self) -> bool:
        try:
            bot_config = config.load_bot_configuration(self._bot_configuration_path)
        except (OSError, ValueError) as xcp:
            log.warning(
                "Disk preference config read failed path=%s: %s: %s",
                self._bot_configuration_path,
                type(xcp).__name__,
                xcp,
            )
            return False

        preferences = bot_config.disk_preferences
        self._configured_activity_mounts = (
            tuple(preferences.activity_mounts) if preferences.activity_mounts is not None else None
        )
        self._configured_labels = dict(preferences.labels)
        self._configured_primary_mount = preferences.primary_mount
        self._configured_secondary_mount = preferences.secondary_mount
        return True

    def set_activity_mounts(self, mountpoints: list[str]) -> tuple[Stats_Disk, ...]:
        with self._lock:
            return self._set_activity_mounts_unlocked(mountpoints)

    def _set_activity_mounts_unlocked(self, mountpoints: list[str]) -> tuple[Stats_Disk, ...]:
        normalised: list[str] = []
        seen_mountpoints: set[str] = set()
        for mountpoint in mountpoints:
            mountpoint_text = config.normalise_absolute_path_text(
                mountpoint,
                source="activity disk mountpoint",
            )
            if mountpoint_text in seen_mountpoints:
                continue
            seen_mountpoints.add(mountpoint_text)
            normalised.append(mountpoint_text)

        unknown_mountpoints = [mountpoint for mountpoint in normalised if mountpoint not in self._disks_by_mountpoint]
        if unknown_mountpoints:
            raise ValueError(f"Unknown activity disk mountpoint(s): {', '.join(unknown_mountpoints)}")

        preferences = self._current_disk_preferences_unlocked()
        preferences.activity_mounts = normalised
        self._set_disk_preferences_unlocked(preferences)
        return self.activity_disks

    def set_primary_mount_override(self, mountpoint: str | None) -> Stats_Disk | None:
        with self._lock:
            return self._set_primary_mount_override_unlocked(mountpoint)

    def _set_primary_mount_override_unlocked(self, mountpoint: str | None) -> Stats_Disk | None:
        normalised_mountpoint = None
        if mountpoint is not None:
            normalised_mountpoint = config.normalise_absolute_path_text(
                mountpoint,
                source="primary disk mountpoint",
            )
            if normalised_mountpoint not in self._disks_by_mountpoint:
                raise ValueError(f"Unknown primary disk mountpoint: {normalised_mountpoint}")

        preferences = self._current_disk_preferences_unlocked()
        preferences.primary_mount = normalised_mountpoint
        self._set_disk_preferences_unlocked(preferences)
        return self.primary_disk

    def set_secondary_mount(self, mountpoint: str | None) -> Stats_Disk | None:
        with self._lock:
            normalised_mountpoint = None
            if mountpoint is not None:
                normalised_mountpoint = config.normalise_absolute_path_text(
                    mountpoint,
                    source="secondary disk mountpoint",
                )
                if normalised_mountpoint not in self._disks_by_mountpoint:
                    raise ValueError(f"Unknown secondary disk mountpoint: {normalised_mountpoint}")
            preferences = self._current_disk_preferences_unlocked()
            preferences.secondary_mount = normalised_mountpoint
            self._set_disk_preferences_unlocked(preferences)
            return self.secondary_disk

    def replace_disk_labels(self, labels_by_mountpoint: Mapping[str, str]) -> tuple[Stats_Disk, ...]:
        with self._lock:
            return self._replace_disk_labels_unlocked(labels_by_mountpoint)

    def _replace_disk_labels_unlocked(self, labels_by_mountpoint: Mapping[str, str]) -> tuple[Stats_Disk, ...]:
        next_labels = {
            mountpoint: label
            for mountpoint, label in self._configured_labels.items()
            if mountpoint not in self._disks_by_mountpoint
        }
        for mountpoint, raw_label in labels_by_mountpoint.items():
            mountpoint_text = config.normalise_absolute_path_text(
                mountpoint,
                source="disk label mountpoint",
            )
            if mountpoint_text not in self._disks_by_mountpoint:
                raise ValueError(f"Unknown disk label mountpoint: {mountpoint_text}")

            label_text = raw_label.strip()
            if label_text:
                next_labels[mountpoint_text] = label_text
            else:
                next_labels.pop(mountpoint_text, None)

        preferences = self._current_disk_preferences_unlocked()
        preferences.labels = next_labels
        self._set_disk_preferences_unlocked(preferences)
        return self.disks

    def set_disk_preferences(
        self,
        preferences: config.PersistedDiskPreferences,
    ) -> config.PersistedDiskPreferences:
        with self._lock:
            return self._set_disk_preferences_unlocked(preferences)

    def _set_disk_preferences_unlocked(
        self,
        preferences: config.PersistedDiskPreferences,
    ) -> config.PersistedDiskPreferences:
        known_mountpoints = set(self._disks_by_mountpoint)
        activity_mounts = preferences.activity_mounts
        if activity_mounts is not None:
            existing_activity_mounts = set(self._configured_activity_mounts or ())
            unknown_activity_mounts = [
                mountpoint
                for mountpoint in activity_mounts
                if mountpoint not in known_mountpoints and mountpoint not in existing_activity_mounts
            ]
            if unknown_activity_mounts:
                raise ValueError(
                    f"Unknown activity disk mountpoint(s): {', '.join(unknown_activity_mounts)}"
                )
        if (
            preferences.primary_mount is not None
            and preferences.primary_mount not in known_mountpoints
            and preferences.primary_mount != self._configured_primary_mount
        ):
            raise ValueError(f"Unknown primary disk mountpoint: {preferences.primary_mount}")
        if (
            preferences.secondary_mount is not None
            and preferences.secondary_mount not in known_mountpoints
            and preferences.secondary_mount != self._configured_secondary_mount
        ):
            raise ValueError(f"Unknown secondary disk mountpoint: {preferences.secondary_mount}")
        new_unknown_label_mounts = (
            set(preferences.labels) - known_mountpoints - set(self._configured_labels)
        )
        if new_unknown_label_mounts:
            raise ValueError(
                "Unknown disk label mountpoint(s): "
                + ", ".join(sorted(new_unknown_label_mounts))
            )

        self._configured_activity_mounts = (
            tuple(activity_mounts) if activity_mounts is not None else None
        )
        self._configured_labels = dict(preferences.labels)
        self._configured_primary_mount = preferences.primary_mount
        self._configured_secondary_mount = preferences.secondary_mount
        self._save_disk_preferences()
        self.refresh_disk_inventory()
        return self._current_disk_preferences_unlocked()

    def _current_disk_preferences_unlocked(self) -> config.PersistedDiskPreferences:
        return config.PersistedDiskPreferences(
            activity_mounts=(
                list(self._configured_activity_mounts)
                if self._configured_activity_mounts is not None
                else None
            ),
            labels=dict(self._configured_labels),
            primary_mount=self._configured_primary_mount,
            secondary_mount=self._configured_secondary_mount,
        )

    def _save_disk_preferences(self) -> None:
        bot_config = config.BotConfiguration()
        if self._bot_configuration_path.exists():
            bot_config = config.load_bot_configuration(self._bot_configuration_path)
        preferences = self._current_disk_preferences_unlocked()
        preferences.labels = dict(sorted(preferences.labels.items()))
        bot_config.disk_preferences = preferences
        config.save_bot_configuration(self._bot_configuration_path, bot_config)

    def refresh_disk_inventory(self) -> None:
        with self._lock:
            self._refresh_disks(force_discovery=True)

    @staticmethod
    def _disk_snapshot(disk: Stats_Disk) -> StatsDiskSnapshot:
        return StatsDiskSnapshot(
            mountpoint_text=disk.mountpoint_text,
            display_name=disk.display_name,
            percent=disk.percent,
            free_bytes=int(disk.usage.free),
            total_bytes=int(disk.usage.total),
        )

    def system_snapshot(self, *, refresh: bool = False) -> StatsSystemSnapshot:
        with self._lock:
            if refresh:
                self._update_unlocked()
            primary_disk = self.primary_disk
            disks = tuple(self._disk_snapshot(disk) for disk in self.disks)
            return StatsSystemSnapshot(
                cpu_percent=self.cpu.r_total,
                cpu_per_core_percent=tuple(self.cpu.r_per_core),
                ram_percent=self.ram.percent,
                ram_used_bytes=int(self.ram.used),
                ram_total_bytes=int(self.ram.raw.total),
                primary_disk=None if primary_disk is None else self._disk_snapshot(primary_disk),
                disks=disks,
            )

    def disk_snapshot_for_path(
        self,
        path: Path,
        *,
        refresh: bool = False,
        fallback_to_primary: bool = True,
    ) -> StatsDiskSnapshot | None:
        with self._lock:
            if refresh:
                self._update_unlocked()
            disk = self.disk_for_path(path)
            if disk is None and fallback_to_primary:
                disk = self.primary_disk
            if disk is None:
                return None
            return self._disk_snapshot(disk)

    def _refresh_disks(self, *, force_discovery: bool = False) -> None:
        if not force_discovery and not self._should_refresh_disk_discovery():
            for disk in self._disks_by_mountpoint.values():
                try:
                    disk.update()
                except OSError:
                    log.warning("Disk stat refresh failed for mountpoint=%s", disk.mountpoint_text, exc_info=True)
            return

        next_disks: dict[str, Stats_Disk] = {}
        for descriptor in self._discover_disk_descriptors():
            mountpoint = descriptor.mountpoint_text
            existing = self._disks_by_mountpoint.get(mountpoint)
            try:
                if existing is None:
                    next_disks[mountpoint] = Stats_Disk(descriptor)
                else:
                    existing.replace_descriptor(descriptor)
                    existing.update()
                    next_disks[mountpoint] = existing
            except OSError:
                log.warning("Disk stat refresh failed for mountpoint=%s", mountpoint, exc_info=True)
        self._disks_by_mountpoint = next_disks
        self._last_disk_discovery_at = datetime.now()

    def _discover_disk_descriptors(self) -> tuple[DiskDescriptor, ...]:
        discovered: dict[str, DiskDescriptor] = {}
        block_devices = self._load_block_device_metadata()
        for partition in psutil.disk_partitions(all=False):
            try:
                mountpoint = config.normalise_absolute_path_text(
                    partition.mountpoint,
                    source="discovered disk mountpoint",
                )
            except ValueError:
                continue

            metadata = block_devices.get(mountpoint) or block_devices.get(partition.device)
            is_bot_disk = self._path_is_within(self._bot_path, Path(mountpoint))
            if self._should_ignore_partition(
                mountpoint=mountpoint,
                filesystem=partition.fstype or None,
                metadata=metadata,
                is_bot_disk=is_bot_disk,
            ):
                continue

            discovered[mountpoint] = DiskDescriptor(
                mountpoint=Path(mountpoint),
                configured_label=self._configured_labels.get(mountpoint),
                device=partition.device or "unknown",
                filesystem=(metadata.filesystem if metadata is not None else partition.fstype) or "unknown",
                options=tuple(option for option in partition.opts.split(",") if option),
                partition_label=metadata.partition_label if metadata is not None else None,
                type_name=(metadata.type_name if metadata is not None else None) or "unknown",
                volume_label=metadata.volume_label if metadata is not None else None,
            )

        if not any(self._path_is_within(self._bot_path, descriptor.mountpoint) for descriptor in discovered.values()):
            fallback = self._fallback_disk_descriptor(self._bot_path)
            discovered.setdefault(fallback.mountpoint_text, fallback)

        if not discovered:
            fallback = self._fallback_disk_descriptor(self._bot_path)
            discovered[fallback.mountpoint_text] = fallback

        return tuple(discovered[mountpoint] for mountpoint in sorted(discovered))

    @staticmethod
    def _fallback_disk_descriptor(path: Path) -> DiskDescriptor:
        resolved = path.resolve(strict=False)
        return DiskDescriptor(
            mountpoint=resolved,
            configured_label=None,
            device="unknown",
            filesystem="unknown",
            options=(),
            partition_label=None,
            type_name="unknown",
            volume_label=None,
        )

    def _should_refresh_disk_discovery(self) -> bool:
        if self._last_disk_discovery_at is None or not self._disks_by_mountpoint:
            return True
        return datetime.now() - self._last_disk_discovery_at >= _DISK_DISCOVERY_REFRESH_INTERVAL

    @staticmethod
    def _should_ignore_partition(
        *,
        mountpoint: str,
        filesystem: str | None,
        metadata: BlockDeviceMetadata | None,
        is_bot_disk: bool,
    ) -> bool:
        if is_bot_disk:
            return False
        mountpoint_casefold = mountpoint.casefold()
        if mountpoint_casefold in _IGNORED_SYSTEM_MOUNTPOINTS:
            return True
        label_candidates = [
            mountpoint_casefold,
            (filesystem or "").casefold(),
        ]
        if metadata is not None:
            label_candidates.extend(
                [
                    (metadata.partition_label or "").casefold(),
                    (metadata.volume_label or "").casefold(),
                ]
            )
        if any("efi" in candidate and candidate for candidate in label_candidates):
            return True
        return False

    @classmethod
    def _load_block_device_metadata(cls) -> dict[str, BlockDeviceMetadata]:
        try:
            response = subprocess.run(
                ["lsblk", "-J", "-o", "PATH,MOUNTPOINT,LABEL,PARTLABEL,FSTYPE,TYPE"],
                capture_output=True,
                check=True,
                text=True,
            )
            payload = json.loads(response.stdout)
        except (OSError, subprocess.CalledProcessError, ValueError) as xcp:
            log.debug("Disk metadata lookup failed: %s", xcp, exc_info=True)
            return {}

        raw_devices = payload.get("blockdevices")
        if not isinstance(raw_devices, list):
            return {}

        metadata_by_key: dict[str, BlockDeviceMetadata] = {}
        for metadata in cls._parse_block_device_metadata_entries(raw_devices):
            if metadata.mountpoint:
                metadata_by_key[metadata.mountpoint] = metadata
            metadata_by_key[metadata.device] = metadata
        return metadata_by_key

    @classmethod
    def _parse_block_device_metadata_entries(cls, entries: list[object]) -> tuple[BlockDeviceMetadata, ...]:
        parsed: list[BlockDeviceMetadata] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            path_value = entry.get("path")
            if not isinstance(path_value, str) or not path_value.strip():
                continue

            mountpoint_value = entry.get("mountpoint")
            filesystem_value = entry.get("fstype")
            label_value = entry.get("label")
            partlabel_value = entry.get("partlabel")
            type_value = entry.get("type")

            parsed.append(
                BlockDeviceMetadata(
                    device=path_value.strip(),
                    filesystem=filesystem_value.strip()
                    if isinstance(filesystem_value, str) and filesystem_value.strip()
                    else None,
                    mountpoint=mountpoint_value.strip()
                    if isinstance(mountpoint_value, str) and mountpoint_value.strip()
                    else None,
                    partition_label=partlabel_value.strip()
                    if isinstance(partlabel_value, str) and partlabel_value.strip()
                    else None,
                    type_name=type_value.strip() if isinstance(type_value, str) and type_value.strip() else None,
                    volume_label=label_value.strip() if isinstance(label_value, str) and label_value.strip() else None,
                )
            )

            child_entries = entry.get("children")
            if isinstance(child_entries, list):
                parsed.extend(cls._parse_block_device_metadata_entries(child_entries))
        return tuple(parsed)

    @staticmethod
    def _path_is_within(path: Path, mountpoint: Path) -> bool:
        try:
            path.relative_to(mountpoint)
        except ValueError:
            return False
        return True

    def _update_unlocked(self) -> None:
        self.cpu.update()
        self.ram.update()
        self._refresh_disks()

    def update(self):
        with self._lock:
            self._update_unlocked()


async def restart(
    ctx: lightbulb.Context | hikari.Message,
    bot: hikari.GatewayBot,
    manager: App_Manager,
    restart_type: str,
    silent: bool = False,
):
    restart_type = restart_type.strip().lower()
    restart_sys = True if restart_type == "system" else False

    mark_pending_process_restart(process_restart_kind(scheduled=False, restart_sys=restart_sys))
    await _prepare_restart(bot=bot, manager=manager)

    if me := bot.get_me():
        bot_name = me.display_name
    else:
        bot_name = config.NAME

    if silent:
        Path("silent_restart").touch()
        await ctx.respond(f"Restarting {restart_type}", flags=hikari.MessageFlag.EPHEMERAL)
    else:
        await bot.update_presence(
            activity=hikari.Activity(name=f"!!! Restarting {restart_type}", type=hikari.ActivityType.CUSTOM),
            status=hikari.Status.DO_NOT_DISTURB,
        )

        mess_id = await ctx.respond(f"{bot_name} restarting {restart_type}")
        if isinstance(mess_id, hikari.Message):
            mess_id = mess_id.id
        Path("restart_message_id").write_text(f"{ctx.channel_id}:{str(mess_id)}")
        await asyncio.sleep(0.1)

    await _finish_restart(restart_sys=restart_sys, ctx=ctx)


async def scheduled_restart(
    *,
    bot: hikari.GatewayBot,
    manager: App_Manager,
    restart_type: str,
    reason: str,
    message_channel_id: hikari.Snowflakeish | None,
    suppress_notifications: bool = False,
    scheduled: bool = True,
    silent: bool = False,
) -> None:
    restart_kind = restart_type.strip().lower()
    restart_sys = restart_kind == "system"
    mark_pending_process_restart(process_restart_kind(scheduled=scheduled, restart_sys=restart_sys))
    if silent:
        Path("silent_restart").touch()
    await _prepare_restart(bot=bot, manager=manager)
    await bot.update_presence(
        activity=hikari.Activity(name=f"!!! Scheduled {restart_kind} restart", type=hikari.ActivityType.CUSTOM),
        status=hikari.Status.DO_NOT_DISTURB,
    )
    if message_channel_id is not None:
        flags: hikari.MessageFlag | hikari.UndefinedType = (
            hikari.MessageFlag.SUPPRESS_NOTIFICATIONS if suppress_notifications else hikari.UNDEFINED
        )
        message = await bot.rest.create_message(
            message_channel_id,
            reason,
            flags=flags,
        )
        Path("restart_message_id").write_text(f"{int(message_channel_id)}:{message.id}")
        await asyncio.sleep(0.1)
    await _finish_restart(restart_sys=restart_sys, ctx=None)


def configure_restart_auto_start_apps(
    manager: App_Manager,
    *,
    enabled: bool,
) -> tuple[str, ...]:
    """Persist the apps to restore after a restart, or explicitly clear that state."""
    if enabled:
        return manager.set_running_restart_auto_start_apps()
    return manager.set_restart_auto_start_apps(())


async def _prepare_restart(
    *,
    bot: hikari.GatewayBot,
    manager: App_Manager,
) -> None:
    try:
        await manager.end()
    except Exception:
        log.warning("Manager shutdown failed", exc_info=True)

    config.IS_RESTARTING = True


async def _finish_restart(
    *,
    restart_sys: bool,
    ctx: lightbulb.Context | hikari.Message | None,
) -> None:
    try:
        if restart_sys:
            reboot_host()
        sys.exit(1)
    except Exception:
        log.exception("Failed to reboot system" if restart_sys else "Failed to restart bot")
        if ctx is not None:
            await ctx.respond(f"unable to {'restart' if restart_sys else 'crash'}")


def reboot_host() -> None:
    result = subprocess.run(["sudo", "systemctl", "reboot", "-i"], check=False)
    log.info("Host reboot command completed: code=%s", result.returncode)
    if result.returncode != 0:
        raise RuntimeError(f"Host reboot command failed with exit code {result.returncode}.")


# AiviA APasz

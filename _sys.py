import asyncio
import json
import logging
import subprocess
import sys
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


class Stats_System(metaclass=Singleton):
    _BOT_CONFIGURATION_PATH = Path("configuration.json")

    def __init__(self):
        self.cpu = Stats_CPU()
        self.ram = Stats_RAM()
        self._bot_path = Path.cwd().resolve()
        self._bot_configuration_path = self._BOT_CONFIGURATION_PATH
        self._configured_activity_mounts: tuple[str, ...] | None = None
        self._configured_labels: dict[str, str] = {}
        self._configured_primary_mount: str | None = None
        self._disks_by_mountpoint: dict[str, Stats_Disk] = {}
        self._last_disk_discovery_at: datetime | None = None
        self.reload_disk_preferences()
        self.refresh_disk_inventory()

    @property
    def disks(self) -> tuple[Stats_Disk, ...]:
        return tuple(self._disks_by_mountpoint[mountpoint] for mountpoint in sorted(self._disks_by_mountpoint))

    @property
    def configured_activity_mounts(self) -> tuple[str, ...] | None:
        return self._configured_activity_mounts

    @property
    def configured_primary_mount(self) -> str | None:
        return self._configured_primary_mount

    @property
    def configured_labels(self) -> Mapping[str, str]:
        return self._configured_labels

    @property
    def bot_disk(self) -> Stats_Disk | None:
        return self.disk_for_path(self._bot_path)

    @property
    def primary_disk_source(self) -> Literal["override", "bot_path", "fallback"]:
        if self._configured_primary_mount is not None and self._configured_primary_mount in self._disks_by_mountpoint:
            return "override"
        if self.bot_disk is not None:
            return "bot_path"
        return "fallback"

    @property
    def primary_disk(self) -> Stats_Disk | None:
        if self._configured_primary_mount is not None:
            disk = self._disks_by_mountpoint.get(self._configured_primary_mount)
            if disk is not None:
                return disk
        bot_disk = self.bot_disk
        if bot_disk is not None:
            return bot_disk
        return self.disks[0] if self.disks else None

    @property
    def disk(self) -> Stats_Disk:
        primary_disk = self.primary_disk
        if primary_disk is None:
            raise RuntimeError("No disks discovered.")
        return primary_disk

    @property
    def activity_disks(self) -> tuple[Stats_Disk, ...]:
        if self._configured_activity_mounts is None:
            return self.disks
        return tuple(
            self._disks_by_mountpoint[mountpoint]
            for mountpoint in self._configured_activity_mounts
            if mountpoint in self._disks_by_mountpoint
        )

    def disk_for_path(self, path: Path) -> Stats_Disk | None:
        resolved = path.resolve(strict=False)
        matches = [disk for disk in self.disks if self._path_is_within(resolved, disk.mountpoint)]
        if not matches:
            return None
        return max(matches, key=lambda disk: len(disk.mountpoint.parts))

    def reload_disk_preferences(self) -> bool:
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
        return True

    def set_activity_mounts(self, mountpoints: list[str]) -> tuple[Stats_Disk, ...]:
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

        self._configured_activity_mounts = tuple(normalised)
        self._save_disk_preferences()
        return self.activity_disks

    def set_primary_mount_override(self, mountpoint: str | None) -> Stats_Disk | None:
        normalised_mountpoint = None
        if mountpoint is not None:
            normalised_mountpoint = config.normalise_absolute_path_text(
                mountpoint,
                source="primary disk mountpoint",
            )
            if normalised_mountpoint not in self._disks_by_mountpoint:
                raise ValueError(f"Unknown primary disk mountpoint: {normalised_mountpoint}")

        self._configured_primary_mount = normalised_mountpoint
        self._save_disk_preferences()
        return self.primary_disk

    def replace_disk_labels(self, labels_by_mountpoint: Mapping[str, str]) -> tuple[Stats_Disk, ...]:
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

        self._configured_labels = next_labels
        self._save_disk_preferences()
        self.refresh_disk_inventory()
        return self.disks

    def _save_disk_preferences(self) -> None:
        bot_config = config.BotConfiguration()
        if self._bot_configuration_path.exists():
            bot_config = config.load_bot_configuration(self._bot_configuration_path)
        bot_config.disk_preferences = config.PersistedDiskPreferences(
            activity_mounts=(
                list(self._configured_activity_mounts) if self._configured_activity_mounts is not None else None
            ),
            labels=dict(sorted(self._configured_labels.items())),
            primary_mount=self._configured_primary_mount,
        )
        config.save_bot_configuration(self._bot_configuration_path, bot_config)

    def refresh_disk_inventory(self) -> None:
        self._refresh_disks(force_discovery=True)

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

    def update(self):
        self.cpu.update()
        self.ram.update()
        self._refresh_disks()


async def restart(
    ctx: lightbulb.Context | hikari.Message,
    bot: hikari.GatewayBot,
    manager: App_Manager,
    restart_type: str,
    silent: bool = False,
):
    restart_type = restart_type.strip().lower()
    restart_sys = True if restart_type == "system" else False

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
) -> None:
    restart_kind = restart_type.strip().lower()
    restart_sys = restart_kind == "system"
    await _prepare_restart(bot=bot, manager=manager)
    await bot.update_presence(
        activity=hikari.Activity(name=f"!!! Scheduled {restart_kind} restart", type=hikari.ActivityType.CUSTOM),
        status=hikari.Status.DO_NOT_DISTURB,
    )
    if message_channel_id is not None:
        message = await bot.rest.create_message(
            message_channel_id,
            reason,
            flags=hikari.MessageFlag.SUPPRESS_NOTIFICATIONS,
        )
        Path("restart_message_id").write_text(f"{int(message_channel_id)}:{message.id}")
        await asyncio.sleep(0.1)
    await _finish_restart(restart_sys=restart_sys, ctx=None)


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
            code = subprocess.run(["sudo", "systemctl", "reboot", "-i"], check=False).returncode
            log.info(f"Restart CMD {code=}")
        sys.exit(1)
    except Exception:
        log.exception("Failed to reboot system" if restart_sys else "Failed to restart bot")
        if ctx is not None:
            await ctx.respond(f"unable to {'restart' if restart_sys else 'crash'}")


# AiviA APasz

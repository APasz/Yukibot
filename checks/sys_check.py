from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
from _activity import Provider_DISK
from _sys import Stats_System, reboot_host


def _disk_partition(mountpoint: str, device: str) -> SimpleNamespace:
    return SimpleNamespace(
        mountpoint=mountpoint,
        device=device,
        fstype="ext4",
        opts="rw",
    )


def _disk_usage(percent: int, *, total: int = 1000) -> SimpleNamespace:
    used = round(total * (percent / 100))
    return SimpleNamespace(
        total=total,
        used=used,
        free=total - used,
        percent=float(percent),
    )


def _lsblk_result(*entries: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(stdout=json.dumps({"blockdevices": list(entries)}))


@pytest.fixture(autouse=True)
def _reset_stats_singleton() -> Iterator[None]:
    config.Singleton._instances.pop(Stats_System, None)
    yield
    config.Singleton._instances.pop(Stats_System, None)


def test_stats_system_discovers_all_disks_and_applies_saved_preferences(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "configuration.json"
    config.save_bot_configuration(
        config_path,
        config.BotConfiguration(
            disk_preferences=config.PersistedDiskPreferences(
                activity_mounts=["/mnt/data"],
                primary_mount="/mnt/backups",
            )
        ),
    )
    usage_by_mountpoint = {
        "/mnt/data": _disk_usage(91),
        "/mnt/backups": _disk_usage(72, total=2000),
    }

    monkeypatch.setattr(Stats_System, "_BOT_CONFIGURATION_PATH", config_path)
    monkeypatch.setattr("_sys.Path.cwd", lambda: Path("/mnt/data/bot"))
    monkeypatch.setattr(
        "_sys.psutil.disk_partitions",
        lambda all=False: [
            _disk_partition("/mnt/data", "/dev/sda1"),
            _disk_partition("/mnt/backups", "/dev/sdb1"),
        ],
    )
    monkeypatch.setattr("_sys.psutil.disk_usage", lambda path: usage_by_mountpoint[str(Path(path))])
    monkeypatch.setattr(
        "_sys.subprocess.run",
        lambda *args, **kwargs: _lsblk_result(
            {
                "path": "/dev/sda1",
                "mountpoint": "/mnt/data",
                "label": "Data",
                "partlabel": None,
                "fstype": "ext4",
                "type": "part",
            },
            {
                "path": "/dev/sdb1",
                "mountpoint": "/mnt/backups",
                "label": "Backups",
                "partlabel": None,
                "fstype": "ext4",
                "type": "part",
            },
        ),
    )

    stats = Stats_System()

    assert [disk.mountpoint_text for disk in stats.disks] == ["/mnt/backups", "/mnt/data"]
    assert stats.bot_disk is not None
    assert stats.bot_disk.mountpoint_text == "/mnt/data"
    assert stats.bot_disk.label == "Data"
    assert stats.primary_disk is not None
    assert stats.primary_disk.mountpoint_text == "/mnt/backups"
    assert stats.primary_disk.label == "Backups"
    assert [disk.mountpoint_text for disk in stats.activity_disks] == ["/mnt/data"]


def test_disk_preferences_are_persisted_without_dropping_other_configuration_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "configuration.json"
    config_path.write_text(json.dumps({"keep": "me"}), encoding="utf-8")
    usage_by_mountpoint = {
        "/mnt/data": _disk_usage(40),
        "/mnt/backups": _disk_usage(55),
    }

    monkeypatch.setattr(Stats_System, "_BOT_CONFIGURATION_PATH", config_path)
    monkeypatch.setattr("_sys.Path.cwd", lambda: Path("/mnt/data/bot"))
    monkeypatch.setattr(
        "_sys.psutil.disk_partitions",
        lambda all=False: [
            _disk_partition("/mnt/data", "/dev/sda1"),
            _disk_partition("/mnt/backups", "/dev/sdb1"),
        ],
    )
    monkeypatch.setattr("_sys.psutil.disk_usage", lambda path: usage_by_mountpoint[str(Path(path))])
    monkeypatch.setattr(
        "_sys.subprocess.run",
        lambda *args, **kwargs: _lsblk_result(
            {
                "path": "/dev/sda1",
                "mountpoint": "/mnt/data",
                "label": "Data",
                "partlabel": None,
                "fstype": "ext4",
                "type": "part",
            },
            {
                "path": "/dev/sdb1",
                "mountpoint": "/mnt/backups",
                "label": "Backups",
                "partlabel": None,
                "fstype": "ext4",
                "type": "part",
            },
        ),
    )

    stats = Stats_System()
    stats.set_activity_mounts(["/mnt/backups"])
    stats.replace_disk_labels({"/mnt/data": "Fast", "/mnt/backups": ""})
    stats.set_primary_mount_override("/mnt/backups")
    stats.set_primary_mount_override(None)
    stats.set_secondary_mount("/mnt/backups")

    payload = json.loads(config_path.read_text(encoding="utf-8"))

    assert payload["keep"] == "me"
    assert payload["disk_preferences"]["activity_mounts"] == ["/mnt/backups"]
    assert payload["disk_preferences"]["labels"] == {"/mnt/data": "Fast"}
    assert payload["disk_preferences"]["primary_mount"] is None
    assert payload["disk_preferences"]["secondary_mount"] == "/mnt/backups"
    assert stats.secondary_disk is not None
    assert stats.secondary_disk.mountpoint_text == "/mnt/backups"


def test_stats_system_ignores_efi_mounts_but_keeps_bot_disk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "configuration.json"
    usage_by_mountpoint = {
        "/": _disk_usage(50, total=2000),
        "/efi": _disk_usage(15, total=256),
        "/mnt/data": _disk_usage(85),
    }

    monkeypatch.setattr(Stats_System, "_BOT_CONFIGURATION_PATH", config_path)
    monkeypatch.setattr("_sys.Path.cwd", lambda: Path("/srv/yukibot"))
    monkeypatch.setattr(
        "_sys.psutil.disk_partitions",
        lambda all=False: [
            _disk_partition("/", "/dev/root"),
            _disk_partition("/efi", "/dev/efi"),
            _disk_partition("/mnt/data", "/dev/sda1"),
        ],
    )
    monkeypatch.setattr("_sys.psutil.disk_usage", lambda path: usage_by_mountpoint[str(Path(path))])
    monkeypatch.setattr(
        "_sys.subprocess.run",
        lambda *args, **kwargs: _lsblk_result(
            {
                "path": "/dev/root",
                "mountpoint": "/",
                "label": "System",
                "partlabel": None,
                "fstype": "ext4",
                "type": "part",
            },
            {
                "path": "/dev/efi",
                "mountpoint": "/efi",
                "label": None,
                "partlabel": "EFI",
                "fstype": "vfat",
                "type": "part",
            },
            {
                "path": "/dev/sda1",
                "mountpoint": "/mnt/data",
                "label": "Data",
                "partlabel": None,
                "fstype": "ext4",
                "type": "part",
            },
        ),
    )

    stats = Stats_System()

    assert [disk.mountpoint_text for disk in stats.disks] == ["/", "/mnt/data"]
    assert stats.bot_disk is not None
    assert stats.bot_disk.mountpoint_text == "/"


@pytest.mark.anyio
async def test_provider_disk_reports_label_for_multi_disk_activity_selection() -> None:
    stats = SimpleNamespace(
        activity_disks=(
            SimpleNamespace(percent=92, mountpoint_text="/mnt/data", display_name="Data"),
            SimpleNamespace(percent=81, mountpoint_text="/mnt/backups", display_name="Backups"),
        )
    )

    provider = Provider_DISK(stats)  # type: ignore[arg-type]

    assert await provider.get() == "Data @ 92"


def test_reboot_host_uses_non_interactive_systemctl(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], bool]] = []

    def run(command: list[str], *, check: bool) -> SimpleNamespace:
        calls.append((command, check))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("_sys.subprocess.run", run)

    reboot_host()

    assert calls == [(["sudo", "systemctl", "reboot", "-i"], False)]


def test_reboot_host_fails_loudly_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("_sys.subprocess.run", lambda command, check: SimpleNamespace(returncode=1))

    with pytest.raises(RuntimeError, match="exit code 1"):
        reboot_host()

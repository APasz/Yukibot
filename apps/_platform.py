"""Typed native platforms shared by app availability and binary selection."""

from __future__ import annotations

import enum
import platform
import sys
from dataclasses import dataclass
from typing import Final


class AppOperatingSystem(enum.StrEnum):
    LINUX = "linux"
    WINDOWS = "win32"
    MACOS = "darwin"

    @property
    def display_name(self) -> str:
        match self:
            case AppOperatingSystem.LINUX:
                return "Linux"
            case AppOperatingSystem.WINDOWS:
                return "Windows"
            case AppOperatingSystem.MACOS:
                return "macOS"


class AppArchitecture(enum.StrEnum):
    X86_64 = "x86_64"
    ARM64 = "arm64"

    @classmethod
    def from_machine(cls, machine: str) -> AppArchitecture:
        """Normalise known machine aliases without guessing unknown architectures."""
        match machine.strip().casefold():
            case "x86_64" | "amd64" | "x64":
                return cls.X86_64
            case "aarch64" | "arm64":
                return cls.ARM64
            case _:
                raise ValueError(f"Unsupported CPU architecture: {machine!r}.")


@dataclass(frozen=True, slots=True)
class AppPlatform:
    operating_system: AppOperatingSystem
    architecture: AppArchitecture

    def __post_init__(self) -> None:
        if not isinstance(self.operating_system, AppOperatingSystem):
            raise TypeError("App platform operating_system must be an AppOperatingSystem.")
        if not isinstance(self.architecture, AppArchitecture):
            raise TypeError("App platform architecture must be an AppArchitecture.")

    @classmethod
    def current(cls) -> AppPlatform:
        """Detect the local native platform; unknown systems fail explicitly."""
        try:
            operating_system = AppOperatingSystem(sys.platform)
        except ValueError:
            raise ValueError(f"Unsupported operating system: {sys.platform!r}.") from None
        return cls(operating_system, AppArchitecture.from_machine(platform.machine()))

    @property
    def display_name(self) -> str:
        return f"{self.operating_system.display_name}/{self.architecture.value}"


LINUX_X86_64: Final[AppPlatform] = AppPlatform(AppOperatingSystem.LINUX, AppArchitecture.X86_64)
LINUX_ARM64: Final[AppPlatform] = AppPlatform(AppOperatingSystem.LINUX, AppArchitecture.ARM64)

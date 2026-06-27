from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import List

import os
import platform
import shutil

# region Environment
class Environment(ABC):
    WINDOWS: str = "Windows"
    MAC: list[str] = ["macOS", "Darwin"]
    LINUX: str = "Linux"

    @classmethod
    def system(cls) -> str:
        return platform.system()

    @classmethod
    def current(cls) -> type[Environment]:
        if cls.is_windows():
            return Windows
        if cls.is_mac():
            return Mac
        if cls.is_linux():
            return Linux
        raise Exception(f"Unsupported platform: {cls.system()}")

    @classmethod
    def is_windows(cls) -> bool:
        return cls.system() == cls.WINDOWS

    @classmethod
    def is_mac(cls) -> bool:
        return cls.system() in cls.MAC

    @classmethod
    def is_linux(cls) -> bool:
        return cls.system() == cls.LINUX

    @classmethod
    @abstractmethod
    def get_disks(cls) -> list[Path]:
        ...

    @classmethod
    def executable(cls, *names: str) -> Path | None:
        for name in names:
            found = shutil.which(name)
            if found:
                return Path(found).resolve()
        return None

    @staticmethod
    def dedupe_paths(paths: list[Path]) -> list[Path]:
        deduped = []
        seen = set()
        for path in paths:
            key = str(path)
            if key not in seen:
                seen.add(key)
                deduped.append(path)
        return deduped


class Windows(Environment):
    NAME: str = Environment.WINDOWS
    @classmethod
    def get_disks(cls) -> list[Path]:
        return [
            Path(f"{drive}:\\")
            for drive in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            if Path(f"{drive}:\\").is_dir() and os.access(f"{drive}:\\", os.R_OK)
        ]

    @classmethod
    def program_files(cls) -> list[Path]:
        dirs = []
        for env_var in ("ProgramFiles", "ProgramFiles(x86)"):
            path = os.environ.get(env_var)
            if path:
                dirs.append(Path(path))

        for disk in cls.get_disks():
            for folder in ("Program Files", "Program Files (x86)"):
                path = disk / folder
                if path.is_dir() and os.access(path, os.R_OK):
                    dirs.append(path)

        return cls.dedupe_paths(dirs)

class Linux(Environment):
    NAME: str = Environment.LINUX

    @classmethod
    def snap_dir(cls) -> Path | None:
        snap_dir = Path("/snap")
        if snap_dir.exists() and snap_dir.is_dir() and os.access(snap_dir, os.R_OK):
            return snap_dir
        return None

    @classmethod
    def homebrew_prefix(cls) -> Path | None:
        prefix = shutil.which("brew")
        if prefix:
            return Path(prefix).resolve().parent.parent
        return None

    @classmethod
    def get_disks(cls) -> list[Path]:
        disks = []
        if not Path("/mnt").exists():
            return disks
        for disk in Path("/mnt").iterdir():
            if disk.is_dir() and os.access(disk, os.R_OK):
                disks.append(disk)
        return cls.dedupe_paths(disks)

class Mac(Environment):
    NAME: List[str] = Environment.MAC

    @classmethod
    def get_disks(cls) -> list[Path]:
        disks = []
        if not Path("/Volumes").exists():
            return disks
        for disk in Path("/Volumes").iterdir():
            if disk.is_dir() and os.access(disk, os.R_OK):
                disks.append(disk)
        return cls.dedupe_paths(disks)
    
    @classmethod
    def homebrew_prefix(cls) -> Path | None:
        prefix = shutil.which("brew")
        if prefix:
            return Path(prefix).resolve().parent.parent
        return None

# endregion Environment

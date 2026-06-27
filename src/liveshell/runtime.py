from __future__ import annotations

import os

from abc import ABC, abstractmethod
from pathlib import Path

# region Session
class Session(ABC):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    @classmethod
    @abstractmethod
    def is_available(cls) -> bool:
        """Return True if the runtime is available on the current system, False otherwise."""
        ...
    
    @abstractmethod
    def is_running(self) -> bool:
        """Return True if the session is running, False otherwise."""
        ...

    @abstractmethod
    def run(self, *args, **kwargs):
        ...

    @abstractmethod
    def text(self, *args, **kwargs) -> str:
        ...

    @abstractmethod
    def close(self, *args, **kwargs):
        ...


class AsyncSession(ABC):
    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.close()

    async def start(self):
        """Start the underlying runtime if it is not already running."""
        return self

    @classmethod
    @abstractmethod
    def is_available(cls) -> bool:
        """Return True if the runtime is available on the current system, False otherwise."""
        ...

    @abstractmethod
    def is_running(self) -> bool:
        """Return True if the session is running, False otherwise."""
        ...

    @abstractmethod
    async def run(self, *args, **kwargs):
        ...

    @abstractmethod
    async def text(self, *args, **kwargs) -> str:
        ...

    @abstractmethod
    async def close(self, *args, **kwargs):
        ...
# endregion Session

# region Mix-Ins
class PreloadableSession(Session):
    @classmethod
    @abstractmethod
    def find(cls, path: str | os.PathLike[str] | None = None, **kwargs) -> Path | None:
        ...
    
    @classmethod
    def is_available(cls, *args, **kwargs) -> bool:
        try:
            return cls.find(*args, **kwargs) is not None
        except RuntimeError:
            return False

    @classmethod
    @abstractmethod
    def is_preloaded(cls) -> bool:
        """Return True if the session engine is loaded into the current process."""
        ...

    @classmethod
    @abstractmethod
    def preload(cls, *args, **kwargs):
        ...


class AsyncPreloadableSession(AsyncSession):
    @classmethod
    @abstractmethod
    def find(cls, path: str | os.PathLike[str] | None = None, **kwargs) -> Path | None:
        ...

    @classmethod
    def is_available(cls, *args, **kwargs) -> bool:
        try:
            return cls.find(*args, **kwargs) is not None
        except RuntimeError:
            return False

    @classmethod
    @abstractmethod
    def is_preloaded(cls) -> bool:
        """Return True if the session engine is loaded into the current process."""
        ...

    @classmethod
    @abstractmethod
    def preload(cls, *args, **kwargs):
        ...

class ProcessBackedSession(Session): ...

class AsyncProcessBackedSession(AsyncSession): ...

class DiscoverableSession(Session): ...

class AsyncDiscoverableSession(AsyncSession): ...
# endregion Mix-Ins

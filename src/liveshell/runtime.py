from __future__ import annotations

import os

from abc import ABC, abstractmethod
from pathlib import Path

from functools import wraps

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
        
    def must_be_running(func):
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            if not self.is_running():
                # raise RuntimeError(f"Session is closed, cannot call {func.__name__}({', '.join(map(str, args))}, {', '.join(f'{k}={v}' for k, v in kwargs.items())})")
                return None
            return func(self, *args, **kwargs)
        return wrapper

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
    @must_be_running
    def run(self, *args, **kwargs):
        ...

    @abstractmethod
    @must_be_running
    def close(self, *args, **kwargs):
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

class ProcessBackedSession(Session): ...

class DiscoverableSession(Session): ...
# endregion Mix-Ins

"""Abstract base class for all social media comment collectors."""

from abc import ABC, abstractmethod
from typing import AsyncIterator, Optional
from src.db.models import MonitoredTarget
from src.schemas.comment import NormalizedComment


class ScraperError(Exception):
    """Explicit collector error that must be surfaced to the API rather than swallowed as 0 comments."""

    def __init__(self, error_code: str, message: Optional[str] = None, status_code: int = 502):
        super().__init__(message or error_code)
        self.error_code = error_code
        self.message = message or error_code
        self.status_code = status_code


class BaseCollector(ABC):
    """Base interface for all platform-specific collectors."""

    platform: str

    @abstractmethod
    async def start(self) -> None:
        """Initialize connections, sessions, or event listeners."""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully shut down connections and release resources."""
        pass

    @abstractmethod
    async def fetch_comments(
        self,
        target: MonitoredTarget,
        cursor: Optional[str] = None,
        limit: int = 100,
    ) -> AsyncIterator[NormalizedComment]:
        """Fetch comments for a given target, yielding NormalizedComment instances."""
        pass

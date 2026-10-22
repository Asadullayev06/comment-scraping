"""Abstract base class for all social media comment collectors."""

from abc import ABC, abstractmethod
from typing import AsyncIterator, Optional
from src.db.models import MonitoredTarget
from src.schemas.comment import NormalizedComment


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

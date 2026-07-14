from abc import ABC, abstractmethod
from typing import List

from ..models import Match


class MatchProvider(ABC):
    """Contract implemented by every match-list data source."""

    @abstractmethod
    async def fetch_matches(self) -> List[Match]:
        raise NotImplementedError

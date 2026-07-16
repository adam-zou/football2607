import asyncio
import unittest
from unittest.mock import Mock

from fetch_data.providers.titan007 import Titan007Provider


class Titan007ProviderTests(unittest.TestCase):
    def test_discovery_uses_row_ids_without_requiring_complete_details(self) -> None:
        class Locator:
            async def evaluate_all(self, expression):
                return ["tr1_12", "tr1_12", "advert", "tr1_34"]

        class Page:
            async def goto(self, url, *, wait_until, timeout):
                return None

            async def wait_for_selector(self, selector, *, state, timeout):
                return None

            async def wait_for_timeout(self, milliseconds):
                return None

            def locator(self, selector):
                return Locator()

        provider = Titan007Provider(
            proxy_manager=Mock(),
            settle_ms=0,
        )

        match_ids = asyncio.run(provider._fetch_match_ids_from_page(Page()))

        self.assertEqual(match_ids, [12, 34])

if __name__ == "__main__":
    unittest.main()

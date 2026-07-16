import unittest
from unittest import mock

from fetch_match_details import block_unneeded_resources, parse_args


class FakeRequest:
    def __init__(self, resource_type: str) -> None:
        self.resource_type = resource_type


class FakeRoute:
    def __init__(self, resource_type: str) -> None:
        self.request = FakeRequest(resource_type)
        self.action = None

    def abort(self) -> None:
        self.action = "abort"

    def continue_(self) -> None:
        self.action = "continue"


class ResourceBlockingTests(unittest.TestCase):
    def test_allows_scripts_that_populate_score_and_status(self) -> None:
        route = FakeRoute("script")

        block_unneeded_resources(route)

        self.assertEqual(route.action, "continue")

    def test_still_blocks_heavy_static_resources(self) -> None:
        for resource_type in ("stylesheet", "image", "media", "font"):
            with self.subTest(resource_type=resource_type):
                route = FakeRoute(resource_type)

                block_unneeded_resources(route)

                self.assertEqual(route.action, "abort")


class DetailConcurrencyArgumentTests(unittest.TestCase):
    def test_defaults_to_two_workers(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            args = parse_args([])

        self.assertEqual(args.concurrency, 2)

    def test_cli_overrides_environment(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"SIMPLE_CRAWLER_DETAIL_CONCURRENCY": "3"},
            clear=True,
        ):
            args = parse_args(["--concurrency", "5"])

        self.assertEqual(args.concurrency, 5)


if __name__ == "__main__":
    unittest.main()

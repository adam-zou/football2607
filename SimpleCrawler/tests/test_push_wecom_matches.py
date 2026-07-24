import json
import unittest
from contextlib import contextmanager
from decimal import Decimal
from unittest import mock

from push_wecom_matches import (
    BASELINE_SQL,
    CREATE_NOTIFICATION_SCHEMA_SQL,
    DISCOVER_SQL,
    PushRecord,
    WeComDeliveryError,
    build_message,
    format_line_value,
    group_deliveries,
    send_wecom_text,
    validate_webhook_url,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self, limit):
        self.limit = limit
        return json.dumps(self.payload).encode("utf-8")


@contextmanager
def response_context(payload):
    yield FakeResponse(payload)


def record(match_id=123, market_type="over_under"):
    return PushRecord(
        match_id=match_id,
        market_type=market_type,
        company_count=3,
        line_value=Decimal("2.50"),
        league="测试联赛",
        scheduled_time="2026-07-23 20:00",
        home_team="主队",
        away_team="客队",
    )


class NotificationSchemaTests(unittest.TestCase):
    def test_push_ledger_is_unique_per_match_and_market(self):
        self.assertIn("PRIMARY KEY (match_id, market_type)", CREATE_NOTIFICATION_SCHEMA_SQL)
        self.assertIn("'baseline'", CREATE_NOTIFICATION_SCHEMA_SQL)
        self.assertIn("match_market_baseline", CREATE_NOTIFICATION_SCHEMA_SQL)

    def test_baseline_and_discovery_only_include_not_started_matches(self):
        self.assertIn("details.status_text = '未开始'", BASELINE_SQL)
        self.assertIn("'baseline'", BASELINE_SQL)
        self.assertNotIn("ids.created_at > state.initialized_at", BASELINE_SQL)
        self.assertIn("details.status_text = '未开始'", DISCOVER_SQL)
        self.assertIn("ids.created_at > state.initialized_at", DISCOVER_SQL)
        self.assertIn("'pending'", DISCOVER_SQL)
        self.assertIn("ON CONFLICT (match_id, market_type) DO NOTHING", DISCOVER_SQL)


class MessageTests(unittest.TestCase):
    def test_groups_markets_by_match(self):
        grouped = group_deliveries(
            [record(123, "over_under"), record(123, "handicap_home"), record(456)]
        )

        self.assertEqual(list(grouped), [123, 456])
        self.assertEqual(len(grouped[123]), 2)

    def test_builds_one_message_with_match_and_market_details(self):
        message = build_message(
            [record(123, "over_under"), record(123, "handicap_home")]
        )

        self.assertIn("测试联赛", message)
        self.assertIn("主队 - 客队", message)
        self.assertIn("大小球（大球）: 3 家, 最大盘口 2.5", message)
        self.assertIn("让球盘（主队）", message)
        self.assertIn("companyid=3&id=123", message)
        self.assertNotIn("###", message)
        self.assertNotIn("**", message)
        self.assertNotIn("> ", message)
        self.assertNotIn("[查看赔率]", message)

    def test_removes_markdown_control_characters_from_match_text(self):
        dirty = PushRecord(
            **{
                **record().__dict__,
                "league": "#联赛*",
                "home_team": "[主队]\n>",
            }
        )

        message = build_message([dirty])

        self.assertIn("联赛", message)
        self.assertIn("主队 - 客队", message)
        for marker in "#*[]`>":
            self.assertNotIn(marker, message)

    def test_formats_zero_and_missing_lines(self):
        self.assertEqual(format_line_value(Decimal("0.00")), "0")
        self.assertEqual(format_line_value(None), "—")


class WebhookTests(unittest.TestCase):
    def test_requires_https_webhook(self):
        with self.assertRaisesRegex(ValueError, "HTTPS URL"):
            validate_webhook_url("http://example.test/hook")

    @mock.patch("push_wecom_matches.urllib.request.urlopen")
    def test_sends_plain_text_and_accepts_confirmed_success(self, urlopen):
        urlopen.return_value = response_context({"errcode": 0, "errmsg": "ok"})

        send_wecom_text("https://example.test/hook", "消息", 3.0)

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["msgtype"], "text")
        self.assertEqual(payload["text"]["content"], "消息")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 3.0)

    @mock.patch("push_wecom_matches.urllib.request.urlopen")
    def test_rejects_business_error_response(self, urlopen):
        urlopen.return_value = response_context(
            {"errcode": 93000, "errmsg": "invalid webhook"}
        )

        with self.assertRaisesRegex(WeComDeliveryError, "93000"):
            send_wecom_text("https://example.test/hook", "消息", 3.0)


if __name__ == "__main__":
    unittest.main()

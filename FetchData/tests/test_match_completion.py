import unittest

from fetch_data.match_completion import mark_matches_completed


class FakeCursor:
    def __init__(self, relations) -> None:
        self.relations = relations
        self.executions = []

    def execute(self, statement, parameters=None) -> None:
        self.executions.append((statement, parameters))

    def fetchone(self):
        return self.relations


class MatchCompletionTests(unittest.TestCase):
    def test_all_three_conditions_are_required(self) -> None:
        cursor = FakeCursor(("match_status", "match_basic_info"))

        mark_matches_completed(cursor, [3020831])

        statement, parameters = cursor.executions[1]
        self.assertIn("basic.status_text = '完'", statement)
        self.assertIn("INTERVAL '3 hours'", statement)
        self.assertIn("handicap_completed", statement)
        self.assertIn("one_x_two_completed", statement)
        self.assertIn("over_under_completed", statement)
        self.assertIn("updated_at = NOW()", statement)
        self.assertIn(") = 6", statement)
        self.assertEqual(parameters, ([3020831],))

    def test_odds_can_be_stored_before_match_tables_exist(self) -> None:
        cursor = FakeCursor((None, None))

        mark_matches_completed(cursor, [3020831])

        self.assertEqual(len(cursor.executions), 1)
        self.assertIn("to_regclass", cursor.executions[0][0])


if __name__ == "__main__":
    unittest.main()

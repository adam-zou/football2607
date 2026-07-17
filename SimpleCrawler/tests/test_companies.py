import unittest

from simple_crawler.companies import (
    COMPANY_IDS,
    COMPANY_NAMES,
    company_label,
)


class CompanyIdentityTests(unittest.TestCase):
    def test_names_are_migrated_in_stable_company_order(self) -> None:
        self.assertEqual(COMPANY_IDS, (3, 4, 8, 24, 31, 47))
        self.assertEqual(
            dict(COMPANY_NAMES),
            {
                3: "Crow*",
                4: "立*",
                8: "36*",
                24: "12*",
                31: "利*",
                47: "平*",
            },
        )

    def test_log_label_contains_id_and_name(self) -> None:
        self.assertEqual(company_label(3), "公司 3（Crow*）")

    def test_unknown_company_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported company_id"):
            company_label(999)


if __name__ == "__main__":
    unittest.main()

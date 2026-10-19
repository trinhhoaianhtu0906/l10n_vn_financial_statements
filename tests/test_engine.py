from datetime import date

from odoo.tests import TransactionCase, tagged

from ..models.engine import FormulaError, ReportEvaluator, Row, comparison_periods, parse_formula


class FakeProvider:
    def __init__(self, env, rows=(), cash=()):
        self._rows = list(rows)
        self._cash = list(cash)
        self.currency = env.ref("base.VND")

    def fiscal_year_start(self, day):
        return date(day.year, 1, 1)

    def rows(self, date_from=None, date_to=None, exclude_closing=False):
        return self._rows

    def cash_rows(self, date_from, date_to):
        return self._cash


@tagged("post_install", "-at_install")
class TestEngine(TransactionCase):
    def _evaluate(self, lines, rows=(), cash=(), kind="position"):
        provider = FakeProvider(self.env, rows, cash)
        evaluator = ReportEvaluator(lines, provider, kind, date(2026, 1, 1), date(2026, 12, 31))
        return evaluator, evaluator.compute()

    def test_parse_rejects_code(self):
        for formula in ("__import__('os')", "BAL(131).real", "open('x')", "BAL(x=1)", "[1] if 1 else 2"):
            with self.assertRaises(FormulaError, msg=formula):
                parse_formula(formula)
        self.assertIsNone(parse_formula("  "))
        parse_formula('[110] + PDR(1311) - NBAL("HN_NCI") * 2')

    def test_partner_split(self):
        rows = [
            Row("1311", 1, 100.0, 0.0),
            Row("1311", 2, 0.0, 30.0),
            Row("1311", 2, 10.0, 0.0),
            Row("1312", 1, 5.0, 0.0),
        ]
        _ev, values = self._evaluate(
            [("131", "PDR(1311)"), ("312", "PCR(1311)"), ("net", "BAL(131)"), ("acc", "DR(1311)")],
            rows,
        )
        self.assertEqual(values["131"], 100.0)
        self.assertEqual(values["312"], 20.0)
        self.assertEqual(values["net"], 85.0)
        self.assertEqual(values["acc"], 80.0)

    def test_line_references_and_cycle(self):
        _ev, values = self._evaluate(
            [("100", "[110] + [120]"), ("110", "BAL(111)"), ("120", ""), ("130", "-[110]")],
            [Row("1111", False, 50.0, 0.0)],
        )
        self.assertEqual(values["100"], 50.0)
        self.assertIsNone(values["120"])
        self.assertEqual(values["130"], -50.0)
        with self.assertRaises(FormulaError):
            self._evaluate([("1", "[2]"), ("2", "[1]")])

    def test_function_not_allowed_for_kind(self):
        with self.assertRaises(FormulaError):
            self._evaluate([("01", "CASH(131)")], kind="position")

    def test_elimination_reduces_natural_side(self):
        rows = [Row("511", False, 0.0, 100.0), Row("511", False, 30.0, 0.0, True)]
        _ev, values = self._evaluate([("01", "CT(511)"), ("02", "DT(511)")], rows, kind="period")
        self.assertEqual(values["01"], 70.0)
        self.assertEqual(values["02"], 0.0)

    def test_cash_rest(self):
        cash = [Row("131", 1, 0.0, 80.0), Row("6421", False, 15.0, 0.0), Row("711", False, 0.0, 4.0)]
        _ev, values = self._evaluate(
            [("01", "CASH(131)"), ("06", "CASH_REST_IN()"), ("07", "CASH_REST_OUT()")],
            cash=cash,
            kind="cashflow",
        )
        self.assertEqual(values["01"], 80.0)
        self.assertEqual(values["06"], 4.0)
        self.assertEqual(values["07"], -15.0)

    def test_comparison_periods(self):
        fy_start = lambda day: date(day.year, 1, 1)  # noqa: E731
        q3 = (date(2026, 7, 1), date(2026, 9, 30))
        self.assertEqual(
            comparison_periods("period", *q3, "previous_period", 2),
            [(date(2026, 4, 1), date(2026, 6, 30)), (date(2026, 1, 1), date(2026, 3, 31))],
        )
        self.assertEqual(
            comparison_periods("period", *q3, "previous_year", 1),
            [(date(2025, 7, 1), date(2025, 9, 30))],
        )
        # B01: the statutory column is the opening of the year, whatever the period.
        self.assertEqual(
            comparison_periods("position", *q3, "default", 2, fy_start),
            [(date(2025, 1, 1), date(2025, 12, 31)), (date(2024, 1, 1), date(2024, 12, 31))],
        )
        self.assertEqual(comparison_periods("position", *q3, "previous_period", 1, fy_start)[0][1], date(2026, 6, 30))
        # February into January keeps month ends.
        self.assertEqual(
            comparison_periods("period", date(2026, 3, 1), date(2026, 3, 31), "previous_period", 1),
            [(date(2026, 2, 1), date(2026, 2, 28))],
        )
        # Days when the period is not whole months.
        self.assertEqual(
            comparison_periods("period", date(2026, 3, 5), date(2026, 3, 14), "previous_period", 1),
            [(date(2026, 2, 23), date(2026, 3, 4))],
        )
        self.assertEqual(comparison_periods("period", *q3, "none", 3), [])

    def test_unmapped_accounts(self):
        evaluator, _values = self._evaluate(
            [("111", "BAL(111)")],
            [Row("1111", False, 10.0, 0.0), Row("1999", False, 7.0, 0.0), Row("511", False, 0.0, 3.0)],
        )
        self.assertEqual(evaluator.unmapped(), {"1999": 7.0})

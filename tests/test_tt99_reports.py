from datetime import date

from odoo.tests import TransactionCase, tagged

from ..models.engine import LedgerProvider, evaluate_report


@tagged("post_install", "-at_install")
class TestTt99Reports(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env["res.company"].create(
            {
                "name": "Công ty TT99 Test",
                "country_id": cls.env.ref("base.vn").id,
                "currency_id": cls.env.ref("base.VND").id,
            }
        )
        cls.env.user.company_ids |= cls.company
        cls.env = cls.env(context=dict(cls.env.context, allowed_company_ids=[cls.company.id]))
        cls.env["account.chart.template"].try_loading("vn", company=cls.company, install_demo=False)
        cls.journal = cls.env["account.journal"].search(
            [("company_id", "=", cls.company.id), ("type", "=", "general")], limit=1
        )
        Partner = cls.env["res.partner"]
        cls.customer_a = Partner.create({"name": "Khách A"})
        cls.customer_b = Partner.create({"name": "Khách B"})
        cls.vendor = Partner.create({"name": "Nhà cung cấp V"})

        cls._post(date(2025, 6, 1), [("112", 20), ("711", -20)])
        cls._post(date(2026, 1, 5), [("112", 1_000_000_000), ("41111", -1_000_000_000)])
        cls._post(date(2026, 2, 1), [("1311", 110, cls.customer_a), ("511", -100), ("33311", -10)])
        cls._post(date(2026, 2, 2), [("112", 50), ("1311", -50, cls.customer_b)])
        cls._post(date(2026, 3, 1), [("156", 60), ("1331", 6), ("3311", -66, cls.vendor)])
        cls._post(date(2026, 3, 2), [("632", 40), ("156", -40)])
        cls._post(date(2026, 3, 3), [("6421", 5), ("112", -5)])
        cls._post(date(2026, 4, 1), [("511", 10), ("1311", -10, cls.customer_a)])

    @classmethod
    def _account(cls, code):
        account = cls.env["account.account"].search(
            [("code", "=", code), ("company_ids", "in", cls.company.id)], limit=1
        )
        if not account:
            account = cls.env["account.account"].search(
                [("code", "=like", code + "%"), ("company_ids", "in", cls.company.id)],
                order="code",
                limit=1,
            )
        assert account, code
        return account

    @classmethod
    def _post(cls, day, items):
        lines = []
        for item in items:
            code, amount = item[0], item[1]
            partner = item[2] if len(item) > 2 else cls.env["res.partner"]
            lines.append(
                (
                    0,
                    0,
                    {
                        "account_id": cls._account(code).id,
                        "partner_id": partner.id,
                        "debit": max(amount, 0),
                        "credit": max(-amount, 0),
                        "name": code,
                    },
                )
            )
        move = cls.env["account.move"].create(
            {"journal_id": cls.journal.id, "date": day, "line_ids": lines}
        )
        move.action_post()
        return move

    def _run(self, xmlid):
        report = self.env.ref(f"l10n_vn_tt99_reports.{xmlid}")
        provider = LedgerProvider(self.env, self.company)
        result = evaluate_report(report, provider, date(2026, 1, 1), date(2026, 12, 31))
        values = {item["line"].code: item["value"] for item in result["lines"]}
        previous = {item["line"].code: item["value_cmp"] for item in result["lines"]}
        return values, previous, result["warnings"]

    def test_b01(self):
        values, opening, warnings = self._run("report_b01")
        self.assertEqual(warnings, [])
        self.assertEqual(values["111"], 1_000_000_065)
        self.assertEqual(values["131"], 100)
        self.assertEqual(values["312"], 50)
        self.assertEqual(values["311"], 66)
        self.assertEqual(values["141"], 20)
        self.assertEqual(values["162"], 6)
        self.assertEqual(values["314"], 10)
        self.assertEqual(values["411a"], 1_000_000_000)
        self.assertEqual(values["420a"], 20)
        self.assertEqual(values["420b"], 45)
        self.assertEqual(values["280"], 1_000_000_191)
        self.assertEqual(values["280"], values["440"])
        self.assertEqual(opening["111"], 20)
        self.assertEqual(opening["420b"], 20)
        self.assertEqual(opening["280"], opening["440"])

    def test_b02(self):
        values, previous, warnings = self._run("report_b02")
        self.assertEqual(warnings, [])
        self.assertEqual(values["01"], 100)
        self.assertEqual(values["02"], 10)
        self.assertEqual(values["10"], 90)
        self.assertEqual(values["11"], 40)
        self.assertEqual(values["26"], 5)
        self.assertEqual(values["60"], 45)
        self.assertEqual(previous["31"], 20)
        self.assertEqual(previous["60"], 20)

    def test_b03_direct_reconciles(self):
        values, _previous, warnings = self._run("report_b03_direct")
        self.assertEqual(warnings, [])
        self.assertEqual(values["01"], 50)
        self.assertEqual(values["02"], -5)
        self.assertEqual(values["31"], 1_000_000_000)
        self.assertEqual(values["60"], 20)
        self.assertEqual(values["70"], 1_000_000_065)

    def test_closing_entries_do_not_change_statements(self):
        before_b01, _o, _w = self._run("report_b01")
        before_b02, _p, _w = self._run("report_b02")
        self._post(date(2026, 12, 31), [("511", 90), ("911", -90)])
        self._post(date(2026, 12, 31), [("911", 45), ("632", -40), ("6421", -5)])
        self._post(date(2026, 12, 31), [("911", 45), ("4212", -45)])
        after_b01, _o, warnings = self._run("report_b01")
        after_b02, _p, _w = self._run("report_b02")
        self.assertEqual(warnings, [])
        self.assertEqual(before_b01, after_b01)
        self.assertEqual(before_b02, after_b02)

    def test_every_chart_account_is_mapped(self):
        """Debit and credit balances on every balance-sheet account still balance B01."""
        accounts = self.env["account.account"].search([("company_ids", "in", self.company.id)])
        partners = [self.customer_a, self.customer_b, self.vendor]
        items = []
        for i, account in enumerate(accounts.sorted("code")):
            if account.code[:1] in "56789":
                continue
            amount = (i % 7 + 1) * 1000 * (1 if i % 2 else -1)
            items.append((account.code, amount, partners[i % 3]))
        items.append(("41111", -sum(amount for _code, amount, _p in items)))
        self._post(date(2026, 6, 30), items)
        values, _opening, warnings = self._run("report_b01")
        self.assertEqual(warnings, [])
        self.assertEqual(values["280"], values["440"])

    def test_unmapped_account_warning(self):
        custom = self.env["account.account"].create(
            {"code": "1999", "name": "Tài khoản tự tạo", "account_type": "asset_current", "company_ids": [self.company.id]}
        )
        self._post(date(2026, 5, 1), [(custom.code, 7), ("112", -7)])
        _values, _opening, warnings = self._run("report_b01")
        self.assertTrue(any("1999" in w for w in warnings), warnings)

    def test_dynamic_report(self):
        Wizard = self.env["vn.report.wizard"]
        choices = Wizard.get_dynamic_options()
        self.assertIn("B01-DN", [r["code"] for r in choices["reports"]])
        data = Wizard.get_dynamic_report(
            {
                "report_id": self.env.ref("l10n_vn_tt99_reports.report_b01").id,
                "company_id": self.company.id,
                "date_from": "2026-01-01",
                "date_to": "2026-12-31",
                "target_move": "posted",
                "unknown_option": 1,
            }
        )
        lines = {line["code"]: line for line in data["lines"]}
        self.assertEqual(lines["280"]["value"], 1_000_000_191)
        self.assertFalse(lines["280"]["drillable"])
        self.assertTrue(lines["131"]["drillable"])
        self.assertEqual(data["col_date"], "31/12/2026")
        line_131 = self.env["vn.report.wizard.line"].browse(lines["131"]["id"])
        action = line_131.action_open_journal_items()
        balances = self.env["account.move.line"].search(action["domain"]).mapped("balance")
        self.assertEqual(sorted(balances), [-50.0, -10.0, 110.0])
        self.assertEqual(Wizard.browse(data["wizard_id"]).action_export_xlsx()["type"], "ir.actions.act_url")

    def test_compare_previous_periods_with_variance(self):
        """B02 for Q1 2026 against the two quarters before, with variance columns."""
        data = self.env["vn.report.wizard"].get_dynamic_report({
            "report_id": self.env.ref("l10n_vn_tt99_reports.report_b02").id,
            "company_id": self.company.id,
            "date_from": "2026-01-01",
            "date_to": "2026-03-31",
            "comparison": "previous_period",
            "comparison_count": 2,
            "show_variance": True,
        })
        self.assertEqual([c["label"] for c in data["columns"]],
                         ["Kỳ này", "Kỳ trước", "Kỳ trước (2)", "Chênh lệch", "%"])
        self.assertEqual(data["columns"][1]["date"], "01/10/2025 - 31/12/2025")
        lines = {line["code"]: line for line in data["lines"]}
        # Q1 2026: revenue 100 (sale in February), nothing in Q4 or Q3 2025.
        self.assertEqual(lines["01"]["cells"][:3], [100, 0, 0])
        self.assertEqual(lines["01"]["cells"][3], 100)
        self.assertIsNone(lines["01"]["cells"][4])
        # Other income 20 was booked in June 2025 (Q2): outside both comparatives.
        self.assertEqual(lines["31"]["cells"][:3], [0, 0, 0])

    def test_b01_quarter_compares_with_year_opening(self):
        data = self.env["vn.report.wizard"].get_dynamic_report({
            "report_id": self.env.ref("l10n_vn_tt99_reports.report_b01").id,
            "company_id": self.company.id,
            "date_from": "2026-04-01",
            "date_to": "2026-06-30",
        })
        self.assertEqual([c["date"] for c in data["columns"]], ["30/06/2026", "31/12/2025"])
        lines = {line["code"]: line for line in data["lines"]}
        self.assertEqual(lines["111"]["cells"], [1_000_000_065, 20])

    def test_wizard_outputs(self):
        wizard = self.env["vn.report.wizard"].create(
            {
                "report_id": self.env.ref("l10n_vn_tt99_reports.report_b01").id,
                "company_id": self.company.id,
                "date_from": date(2026, 1, 1),
                "date_to": date(2026, 12, 31),
            }
        )
        wizard.action_compute()
        line_280 = wizard.line_ids.filtered(lambda line: line.code == "280")
        self.assertEqual(line_280.value, 1_000_000_191)
        self.assertEqual(wizard.format_amount(-1234567), "(1.234.567)")
        action = wizard.action_export_xlsx()
        self.assertEqual(action["type"], "ir.actions.act_url")
        self.assertTrue(wizard.xlsx_file)
        html = self.env["ir.actions.report"]._render_qweb_html(
            "l10n_vn_tt99_reports.report_vn_financial", wizard.ids
        )[0]
        self.assertIn("BÁO CÁO TÌNH HÌNH TÀI CHÍNH", html.decode())
        self.assertIn("1.000.000.191", html.decode())

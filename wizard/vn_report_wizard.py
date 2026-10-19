import base64
import io

import xlsxwriter

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools.translate import LazyTranslate

from ..models.engine import (
    COMPARE_DEFAULT,
    COMPARE_LAST_YEAR,
    COMPARE_NONE,
    COMPARE_PREVIOUS,
    OTHER_LEDGER,
    LedgerProvider,
    comparison_periods,
    evaluate_report,
    iter_calls,
    parse_formula,
    with_companies,
)

_lt = LazyTranslate(__name__)

COLUMN_LABELS = {
    "position": (_lt("Closing Balance"), _lt("Opening Balance")),
    "period": (_lt("This Year"), _lt("Last Year")),
    "cashflow": (_lt("This Year"), _lt("Last Year")),
}


COMPARISON_LABELS = {COMPARE_PREVIOUS: _lt("Previous Period"), COMPARE_LAST_YEAR: _lt("Same Period Last Year")}


def format_percent(value):
    if value is None:
        return ""
    return f"{value:,.1f}%".replace(",", "#").replace(".", ",").replace("#", ".")


def format_amount(value):
    """1234567 -> '1.234.567'; negatives in brackets, as Circular 99 requires."""
    if value is None or value is False:
        return ""
    text = f"{abs(round(value)):,.0f}".replace(",", ".")
    return f"({text})" if value < 0 else text


class VnReportWizard(models.TransientModel):
    _name = "vn.report.wizard"
    _description = "Vietnamese financial statement wizard"

    def _default_dates(self, key):
        dates = self.env.company.compute_fiscalyear_dates(fields.Date.context_today(self))
        return dates[key]

    report_id = fields.Many2one(
        "vn.report",
        string="Report",
        required=True,
        domain="[('consolidated', '=', False)]",
    )
    report_kind = fields.Selection(related="report_id.kind")
    company_id = fields.Many2one(
        "res.company",
        string="Company",
        required=True,
        default=lambda self: self.env.company,
        domain=lambda self: [("id", "in", self.env.companies.ids)],
    )
    include_branches = fields.Boolean(
        string="Include Branches",
        default=True,
        help="Add the figures of the branches (child companies in Odoo).",
    )
    date_from = fields.Date(
        string="Date From", required=True, default=lambda self: self._default_dates("date_from")
    )
    date_to = fields.Date(
        string="Date To", required=True, default=lambda self: self._default_dates("date_to")
    )
    target_move = fields.Selection(
        [("posted", "Posted Entries"), ("all", "All Entries (including drafts)")],
        string="Entries",
        default="posted",
        required=True,
    )
    hide_zero = fields.Boolean(
        string="Hide Empty Lines",
        help="Circular 99: lines without figures may be left out, but the line codes are not renumbered.",
    )
    comparison = fields.Selection(
        [
            (COMPARE_DEFAULT, "Statutory Form (opening of the year / same period last year)"),
            (COMPARE_PREVIOUS, "Previous Period"),
            (COMPARE_LAST_YEAR, "Same Period Last Year"),
            (COMPARE_NONE, "No Comparison"),
        ],
        string="Compare With",
        default=COMPARE_DEFAULT,
        required=True,
    )
    comparison_count = fields.Integer(string="Number of Periods", default=1)
    show_variance = fields.Boolean(
        string="Show Variance", help="Add variance and % columns against the first comparison period."
    )
    comparison_dates = fields.Json(readonly=True)
    line_ids = fields.One2many("vn.report.wizard.line", "wizard_id", string="Results")
    warning_text = fields.Text(string="Warnings", readonly=True)
    computed = fields.Boolean(readonly=True)
    col_label = fields.Char(compute="_compute_col_labels")
    col_cmp_label = fields.Char(compute="_compute_col_labels")
    xlsx_file = fields.Binary(readonly=True, attachment=False)
    xlsx_filename = fields.Char()

    @api.depends("report_id.kind")
    def _compute_col_labels(self):
        for wizard in self:
            labels = COLUMN_LABELS.get(wizard.report_id.kind or "position")
            wizard.col_label, wizard.col_cmp_label = (self.env._(label) for label in labels)

    @api.constrains("comparison_count")
    def _check_comparison_count(self):
        for wizard in self:
            if not 1 <= wizard.comparison_count <= 6:
                raise UserError(self.env._("The number of comparison periods must be between 1 and 6."))

    @api.onchange(
        "report_id", "company_id", "date_from", "date_to", "target_move",
        "comparison", "comparison_count", "show_variance",
    )
    def _onchange_reset(self):
        self.line_ids = [fields.Command.clear()]
        self.computed = False
        self.warning_text = False

    # -- dynamic report (client action) ---------------------------------

    _DYNAMIC_OPTIONS = (
        "report_id",
        "company_id",
        "date_from",
        "date_to",
        "target_move",
        "hide_zero",
        "include_branches",
        "consolidation_group_id",
        "comparison",
        "comparison_count",
        "show_variance",
    )

    @api.model
    def get_dynamic_options(self):
        """Choices for the filters of the on-screen report."""
        dates = self.env.company.compute_fiscalyear_dates(fields.Date.context_today(self))
        return {
            "reports": self.env["vn.report"].search_read([], ["name", "code", "kind", "consolidated", "ledger"]),
            "companies": [{"id": c.id, "name": c.name} for c in self.env.user.sudo().company_ids],
            "company_id": self.env.company.id,
            "date_from": fields.Date.to_string(dates["date_from"]),
            "date_to": fields.Date.to_string(dates["date_to"]),
            "groups": self._dynamic_groups(),
        }

    @api.model
    def _dynamic_groups(self):
        return []

    @api.model
    def get_dynamic_report(self, options):
        """Compute a report for the on-screen view; the wizard id serves the exports."""
        vals = {key: value for key, value in options.items() if key in self._DYNAMIC_OPTIONS and key in self._fields}
        wizard = self.create(vals)
        wizard._compute_report()
        report = wizard.report_id
        col_date, col_cmp_date = wizard.column_dates()
        columns = wizard.column_headers()
        return {
            "wizard_id": wizard.id,
            "title": report.title,
            "subtitle": report.subtitle or "",
            "form_code": report.form_code or "",
            "company": wizard.company_id.name,
            "currency": wizard.company_id.currency_id.name,
            "period_text": wizard.period_text(),
            "col_label": wizard.col_label,
            "col_cmp_label": wizard.col_cmp_label,
            "col_date": col_date,
            "col_cmp_date": col_cmp_date,
            "columns": columns,
            "show_codes": report.show_codes,
            "warnings": (wizard.warning_text or "").splitlines(),
            "lines": [
                {
                    "id": line.id,
                    "code": line.display_code or "",
                    "name": line.name,
                    "note_ref": line.note_ref or "",
                    "level": line.level,
                    "bold": line.bold,
                    "blank": line.blank,
                    "value": line.value,
                    "value_cmp": line.value_cmp,
                    "cells": line.cells(),
                    "drillable": bool(line.formula) and not line.formula.lstrip().startswith("["),
                }
                for line in wizard.line_ids
            ],
        }

    # -- computation -----------------------------------------------------

    def _drilldown_companies(self):
        return self._get_provider().companies

    def _get_provider(self):
        companies = self.company_id
        if self.include_branches:
            env = with_companies(self.env, self.company_id)
            companies = env["res.company"].search([("id", "child_of", self.company_id.id)])
            companies &= self.env.user.sudo().company_ids  # branches the user may read
        return LedgerProvider(
            self.env, companies, posted_only=self.target_move == "posted", ledger=self.report_id.ledger or "vas"
        )

    def _compute_report(self):
        self.ensure_one()
        provider = self._get_provider()
        comparisons = comparison_periods(
            self.report_id.kind,
            self.date_from,
            self.date_to,
            self.comparison,
            self.comparison_count,
            provider.fiscal_year_start,
        )
        result = evaluate_report(
            self.report_id, provider, self.date_from, self.date_to, comparisons=comparisons
        )
        commands = [fields.Command.clear()]
        for item in result["lines"]:
            line, value, value_cmps = item["line"], item["value"], item["value_cmps"]
            value_cmp = item["value_cmp"]
            blank = value is None and all(v is None for v in value_cmps)
            if (self.hide_zero or line.hide_if_zero) and line.formula and not value and not any(value_cmps):
                continue
            commands.append(
                fields.Command.create(
                    {
                        "report_line_id": line.id,
                        "code": line.code,
                        "name": line.name,
                        "note_ref": line.note_ref,
                        "level": line.level,
                        "bold": line.bold,
                        "blank": blank,
                        "value": value or 0.0,
                        "value_cmp": value_cmp or 0.0,
                        "values_cmp": [v or 0.0 for v in value_cmps],
                    }
                )
            )
        self.write(
            {
                "line_ids": commands,
                "warning_text": "\n".join(result["warnings"]) or False,
                "computed": True,
                "comparison_dates": [[str(f), str(t)] for f, t in comparisons],
            }
        )

    def _reopen(self):
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
            "name": self.report_id.name,
        }

    def action_compute(self):
        self._compute_report()
        return self._reopen()

    def _ensure_computed(self):
        # The on-screen report keeps the line ids for drill-down: don't rebuild them.
        if not self.computed:
            self._compute_report()

    def action_print_pdf(self):
        self._ensure_computed()
        return self.env.ref("l10n_vn_financial_statements.action_report_vn_financial").report_action(self, config=False)

    def action_export_xlsx(self):
        self._ensure_computed()
        self.write(
            {
                "xlsx_file": base64.b64encode(self._render_xlsx()),
                "xlsx_filename": self._export_filename("xlsx"),
            }
        )
        return {
            "type": "ir.actions.act_url",
            "url": f"/web/content/?model={self._name}&id={self.id}&field=xlsx_file"
            f"&filename_field=xlsx_filename&download=true",
            "target": "self",
        }

    # -- presentation helpers (used by QWeb and XLSX) ---------------------

    def _export_filename(self, ext=None):
        code = (self.report_id.code or "report").replace("/", "-")
        name = f"{code}_{self.date_to}"
        return f"{name}.{ext}" if ext else name

    def format_amount(self, value, blank=False):
        return "" if blank else format_amount(value)

    def period_text(self):
        """Second title line, as printed on the statutory forms."""
        d_to = self.date_to
        if self.report_id.kind == "position":
            return self.env._(
                "As at %(day)s/%(month)s/%(year)s", day=f"{d_to.day:02d}", month=f"{d_to.month:02d}", year=d_to.year
            )
        d_from = self.date_from
        return self.env._(
            "Accounting period from %(date_from)s to %(date_to)s",
            date_from=f"{d_from:%d/%m/%Y}",
            date_to=f"{d_to:%d/%m/%Y}",
        )

    def _period_label(self, date_from, date_to):
        if self.report_id.kind == "position":
            return f"{date_to:%d/%m/%Y}"
        return f"{date_from:%d/%m/%Y} - {date_to:%d/%m/%Y}"

    def column_headers(self):
        """Amount columns: [{label, date, kind}]: current, comparisons, then variance."""
        default_labels = COLUMN_LABELS.get(self.report_id.kind or "position")
        periods = [(fields.Date.to_date(f), fields.Date.to_date(t)) for f, t in self.comparison_dates or []]
        if self.comparison == COMPARE_DEFAULT:
            current = self.env._(default_labels[0])
        elif self.report_id.kind == "position":
            current = self.env._("End of Period")
        else:
            current = self.env._("This Period")
        columns = [{"label": current, "date": self._period_label(self.date_from, self.date_to), "kind": "amount"}]
        for index, (date_from, date_to) in enumerate(periods, start=1):
            if self.comparison == COMPARE_DEFAULT:
                label = self.env._(default_labels[1])
            else:
                label = self.env._(COMPARISON_LABELS[self.comparison])
            if index > 1:
                label = f"{label} ({index})"
            columns.append({"label": label, "date": self._period_label(date_from, date_to), "kind": "amount"})
        if self.show_variance and periods:
            columns.append({"label": self.env._("Variance"), "date": "(4) - (5)", "kind": "amount"})
            columns.append({"label": "%", "date": "(4) / (5) - 1", "kind": "percent"})
        return columns

    def column_dates(self):
        """Dates under the first two columns (kept for the dialog view)."""
        columns = self.column_headers()
        return columns[0]["date"], columns[1]["date"] if len(columns) > 1 else ""

    def format_cell(self, value, kind, blank=False):
        if blank:
            return ""
        return format_percent(value) if kind == "percent" else format_amount(value)

    def _render_xlsx(self):
        buffer = io.BytesIO()
        book = xlsxwriter.Workbook(buffer, {"in_memory": True})
        sheet = book.add_worksheet(self.report_id.code[:31].replace("/", "-"))
        base = {"font_name": "Times New Roman", "font_size": 11}
        fmt = {
            "text": book.add_format(base),
            "bold": book.add_format({**base, "bold": True}),
            "title": book.add_format({**base, "bold": True, "font_size": 14, "align": "center"}),
            "center": book.add_format({**base, "align": "center", "italic": True}),
            "right": book.add_format({**base, "align": "right", "italic": True}),
            "head": book.add_format(
                {**base, "bold": True, "align": "center", "valign": "vcenter", "border": 1, "text_wrap": True}
            ),
            "cell": book.add_format({**base, "border": 1, "text_wrap": True}),
            "cell_b": book.add_format({**base, "border": 1, "bold": True, "text_wrap": True}),
            "code": book.add_format({**base, "border": 1, "align": "center"}),
            "num": book.add_format({**base, "border": 1, "num_format": "#,##0;(#,##0);-"}),
            "num_b": book.add_format({**base, "border": 1, "bold": True, "num_format": "#,##0;(#,##0);-"}),
        }
        columns = self.column_headers()
        last = 2 + len(columns)
        fmt["pct"] = book.add_format({**base, "border": 1, "num_format": "0.0%;(0.0%);-"})
        fmt["pct_b"] = book.add_format({**base, "border": 1, "bold": True, "num_format": "0.0%;(0.0%);-"})
        fmt["head_date"] = book.add_format({**base, "italic": True, "align": "center", "border": 1, "font_size": 9})
        sheet.set_column(0, 0, 60)
        sheet.set_column(1, 2, 10)
        sheet.set_column(3, last, 20)

        company = self.company_id
        report = self.report_id
        address = ", ".join(filter(None, [company.street, company.street2, company.city, company.state_id.name]))
        sheet.write(0, 0, self.env._("Reporting entity: %s", company.name), fmt["bold"])
        sheet.write(1, 0, self.env._("Address: %s", address), fmt["text"])
        right = max(last, 4)
        sheet.merge_range(0, 2, 0, right, report.form_code or "", fmt["bold"])
        sheet.merge_range(1, 2, 2, right, report.form_note or "", book.add_format({**base, "italic": True, "text_wrap": True}))
        sheet.merge_range(4, 0, 4, right, report.title, fmt["title"])
        row = 5
        if report.subtitle:
            sheet.merge_range(row, 0, row, right, report.subtitle, fmt["center"])
            row += 1
        sheet.merge_range(row, 0, row, right, self.period_text(), fmt["center"])
        row += 1
        sheet.merge_range(row, 0, row, right, self.env._("Currency: %s", company.currency_id.name), fmt["right"])
        row += 2
        code_label = self.env._("Code") if report.show_codes else ""
        headers = [self.env._("Line"), code_label, self.env._("Note")] + [c["label"] for c in columns]
        for i, label in enumerate(headers):
            sheet.write(row, i, label, fmt["head"])
        row += 1
        for i in range(3):
            sheet.write_blank(row, i, None, fmt["head_date"])
        for i, column in enumerate(columns, start=3):
            sheet.write(row, i, column["date"], fmt["head_date"])
        row += 1
        if report.show_codes:
            for i in range(len(headers)):
                sheet.write(row, i, str(i + 1), fmt["head"])
            row += 1
        for line in self.line_ids:
            indent = "    " * max(line.level - 1, 0)
            sheet.write(row, 0, indent + line.name, fmt["cell_b"] if line.bold else fmt["cell"])
            sheet.write(row, 1, line.display_code, fmt["code"])
            sheet.write(row, 2, line.note_ref or "", fmt["code"])
            for i, (column, value) in enumerate(zip(columns, line.cells(), strict=True), start=3):
                if column["kind"] == "percent":
                    cell_format = fmt["pct_b"] if line.bold else fmt["pct"]
                    value = None if value is None else value / 100
                else:
                    cell_format = fmt["num_b"] if line.bold else fmt["num"]
                if line.blank or value is None:
                    sheet.write_blank(row, i, None, cell_format)
                else:
                    sheet.write_number(row, i, value, cell_format)
            row += 1
        row += 2
        signers = [self.env._("PREPARED BY"), self.env._("CHIEF ACCOUNTANT"), self.env._("LEGAL REPRESENTATIVE")]
        sheet.merge_range(row, 2, row, right, self.env._("Approved on ......"), fmt["center"])
        row += 1
        for i, (label, col_idx) in enumerate(zip(signers, (0, 2, 3), strict=True)):
            sheet.write(row, col_idx, label, fmt["bold"])
            signature = self.env._("(Signature, full name, seal)") if i == 2 else self.env._("(Signature, full name)")
            sheet.write(row + 1, col_idx, signature, fmt["text"])
        if self.warning_text:
            row += 4
            sheet.write(row, 0, self.env._("Check warnings:"), fmt["bold"])
            for warning in self.warning_text.splitlines():
                row += 1
                sheet.write(row, 0, warning, fmt["text"])
        book.close()
        return buffer.getvalue()


class VnReportWizardLine(models.TransientModel):
    _name = "vn.report.wizard.line"
    _description = "Vietnamese financial statement result line"
    _order = "id"

    wizard_id = fields.Many2one("vn.report.wizard", required=True, ondelete="cascade")
    report_line_id = fields.Many2one("vn.report.line", readonly=True)
    code = fields.Char(string="Code", readonly=True)
    name = fields.Char(string="Line", readonly=True)
    note_ref = fields.Char(string="Note", readonly=True)
    level = fields.Integer(readonly=True)
    bold = fields.Boolean(readonly=True)
    blank = fields.Boolean(readonly=True)
    value = fields.Float(string="This Period", readonly=True, digits=(16, 0))
    value_cmp = fields.Float(string="Comparison", readonly=True, digits=(16, 0))
    values_cmp = fields.Json(readonly=True)
    formula = fields.Char(related="report_line_id.formula")
    display_code = fields.Char(compute="_compute_display_code")

    @api.depends("code", "wizard_id.report_id.show_codes")
    def _compute_display_code(self):
        # Section headers (I, II, III) carry a technical code, not a printed line code.
        for line in self:
            hidden = not line.wizard_id.report_id.show_codes or (line.code or "").isalpha()
            line.display_code = "" if hidden else line.code

    def cells(self):
        """Amounts of every column of ``wizard.column_headers()`` (None: empty cell)."""
        self.ensure_one()
        comparisons = list(self.values_cmp or [])
        cells = [self.value] + comparisons
        if self.wizard_id.show_variance and comparisons:
            base = comparisons[0]
            cells.append(self.value - base)
            cells.append((self.value - base) / abs(base) * 100 if base else None)
        return cells

    def get_drilldown(self):
        """Drill-down action, plus the companies of the report hidden by the company switcher.

        The list view only shows the companies active in the switcher; the report
        itself reads all of them, so tell the user which ones to switch on.
        """
        self.ensure_one()
        active = self.env.context.get("allowed_company_ids") or self.env.companies.ids
        inactive = self.wizard_id._drilldown_companies().filtered(lambda c: c.id not in active)
        return {"action": self.action_open_journal_items(), "inactive_companies": inactive.sudo().mapped("name")}

    def action_open_journal_items(self):
        """Drill down: journal items of the accounts a line reads."""
        self.ensure_one()
        prefixes = {p for _name, args in iter_calls(parse_formula(self.formula)) for p in args}
        prefixes = {p for p in prefixes if p[:1].isdigit()}
        if not prefixes:
            raise UserError(self.env._("This line does not read any account directly."))
        wizard = self.wizard_id
        companies = wizard._drilldown_companies()
        env = with_companies(self.env, companies)
        account_ids = []
        for root in companies.root_id:
            accounts = env["account.account"].with_company(root).search([("company_ids", "in", root.id)])
            account_ids += [a.id for a in accounts if (a.code or "").startswith(tuple(prefixes))]
        domain = [
            ("account_id", "in", account_ids),
            ("journal_id.vn_ledger", "!=", OTHER_LEDGER[wizard.report_id.ledger or "vas"]),
            ("company_id", "in", companies.ids),
            ("date", "<=", wizard.date_to),
            ("parent_state", "in", ("posted",) if wizard.target_move == "posted" else ("draft", "posted")),
        ]
        if wizard.report_id.kind != "position":
            domain.append(("date", ">=", wizard.date_from))
        return {
            "type": "ir.actions.act_window",
            "name": f"[{self.code}] {self.name}",
            "res_model": "account.move.line",
            "view_mode": "list,form",
            "views": [(False, "list"), (False, "form")],
            "domain": domain,
            "context": {"search_default_group_by_account": 1},
        }

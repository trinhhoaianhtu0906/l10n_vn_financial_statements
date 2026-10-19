from odoo import api, fields, models
from odoo.exceptions import ValidationError

from .engine import FormulaError, parse_formula


class VnReport(models.Model):
    _name = "vn.report"
    _description = "Vietnamese financial statement"
    _order = "sequence, code"

    name = fields.Char(required=True, translate=True)
    code = fields.Char(required=True, help="Form code, e.g. B01-DN")
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    kind = fields.Selection(
        [
            ("position", "Financial position (balances at a date)"),
            ("period", "Income statement (movements of the period)"),
            ("cashflow", "Cash flows"),
        ],
        required=True,
    )
    ledger = fields.Selection(
        [("vas", "VAS"), ("ifrs", "IFRS")],
        default="vas",
        required=True,
        help="VAS reads the books without the IFRS adjustment journals; IFRS adds them.",
    )
    show_codes = fields.Boolean(
        string="Show Line Codes",
        default=True,
        help="Vietnamese statutory forms print the line code column and the column numbers.",
    )
    consolidated = fields.Boolean(
        help="Consolidated statement: runs on a consolidation group only."
    )
    form_code = fields.Char(string="Form", translate=True, help="E.g. Form B 01 - DN")
    form_note = fields.Char(
        string="Legal Basis",
        translate=True,
        default="(Issued with Circular No. 99/2025/TT-BTC of 27 October 2025"
        " of the Minister of Finance)",
    )
    title = fields.Char(required=True, translate=True, help="Title printed on the statement")
    subtitle = fields.Char(translate=True, help="Line under the title, e.g. the method used")
    check_expression = fields.Char(
        string="Check Formula",
        help="Expression that must equal 0, e.g. [280] - [440]. Otherwise the report shows a warning.",
    )
    check_message = fields.Char(string="Check Message", translate=True)
    line_ids = fields.One2many("vn.report.line", "report_id", string="Lines", copy=True)

    _code_uniq = models.Constraint("UNIQUE(code)", "The report code must be unique.")

    def copy_data(self, default=None):
        vals_list = super().copy_data(default=default)
        for report, vals in zip(self, vals_list, strict=True):
            vals.setdefault("code", f"{report.code}-copy")
            vals.setdefault("name", self.env._("%s (copy)", report.name))
        return vals_list

    @api.constrains("check_expression")
    def _check_check_expression(self):
        for report in self:
            try:
                parse_formula(report.check_expression)
            except FormulaError as e:
                raise ValidationError(e.message(self.env)) from e

    def action_open_wizard(self):
        self.ensure_one()
        return {
            "type": "ir.actions.client",
            "name": self.name,
            "tag": "l10n_vn_financial_statements.dynamic_report",
            "context": {"default_report_id": self.id},
        }


class VnReportLine(models.Model):
    _name = "vn.report.line"
    _description = "Vietnamese financial statement line"
    _order = "report_id, sequence, id"

    report_id = fields.Many2one("vn.report", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer(default=10)
    code = fields.Char(string="Code", required=True)
    name = fields.Char(string="Line", required=True, translate=True)
    note_ref = fields.Char(string="Note")
    formula = fields.Char(
        string="Formula",
        help="E.g. PDR(1311): debit balances of account 1311 per partner; "
        "[110] + [120]: sum of other lines. Leave empty for a line filled in by hand.",
    )
    level = fields.Integer(default=2, help="0: part, 1: roman numeral section, 2: line, 3: detail line")
    bold = fields.Boolean()
    hide_if_zero = fields.Boolean(
        string="Hide When Zero",
        help="Circular 99 allows leaving out lines without figures (their codes are kept).",
    )

    _code_uniq = models.Constraint(
        "UNIQUE(report_id, code)", "A line code must be unique within a report."
    )

    @api.constrains("formula")
    def _check_formula(self):
        for line in self:
            try:
                parse_formula(line.formula)
            except FormulaError as e:
                raise ValidationError(f"[{line.code}] {e.message(self.env)}") from e

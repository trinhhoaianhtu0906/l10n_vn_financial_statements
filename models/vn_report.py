from odoo import api, fields, models
from odoo.exceptions import ValidationError

from .engine import FormulaError, parse_formula


class VnReport(models.Model):
    _name = "vn.report"
    _description = "TT99 financial statement"
    _order = "sequence, code"

    name = fields.Char(required=True, translate=True)
    code = fields.Char(required=True, help="Ký hiệu mẫu, ví dụ B01-DN")
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    kind = fields.Selection(
        [
            ("position", "Tình hình tài chính (số dư tại ngày)"),
            ("period", "Kết quả kinh doanh (phát sinh trong kỳ)"),
            ("cashflow", "Lưu chuyển tiền tệ"),
        ],
        required=True,
    )
    consolidated = fields.Boolean(
        help="Báo cáo hợp nhất: chỉ chạy được trên một nhóm hợp nhất."
    )
    form_code = fields.Char(string="Mẫu số", help="Ví dụ: Mẫu số B 01 - DN")
    form_note = fields.Char(
        string="Căn cứ",
        default="(Kèm theo Thông tư số 99/2025/TT-BTC ngày 27 tháng 10 năm 2025"
        " của Bộ trưởng Bộ Tài chính)",
    )
    title = fields.Char(required=True, help="Tiêu đề in trên báo cáo")
    subtitle = fields.Char(help="Dòng phụ dưới tiêu đề, ví dụ phương pháp lập")
    check_expression = fields.Char(
        string="Công thức kiểm tra",
        help="Biểu thức phải bằng 0, ví dụ [280] - [440]. Nếu khác 0, báo cáo hiện cảnh báo.",
    )
    check_message = fields.Char(string="Thông báo khi lệch")
    line_ids = fields.One2many("vn.report.line", "report_id", string="Chỉ tiêu", copy=True)

    _code_uniq = models.Constraint("UNIQUE(code)", "Ký hiệu mẫu báo cáo phải là duy nhất.")

    def copy_data(self, default=None):
        vals_list = super().copy_data(default=default)
        for report, vals in zip(self, vals_list, strict=True):
            vals.setdefault("code", f"{report.code}-copy")
            vals.setdefault("name", self.env._("%s (bản sao)", report.name))
        return vals_list

    @api.constrains("check_expression")
    def _check_check_expression(self):
        for report in self:
            try:
                parse_formula(report.check_expression)
            except FormulaError as e:
                raise ValidationError(str(e)) from e

    def action_open_wizard(self):
        self.ensure_one()
        return {
            "type": "ir.actions.client",
            "name": self.name,
            "tag": "l10n_vn_tt99_reports.dynamic_report",
            "context": {"default_report_id": self.id},
        }


class VnReportLine(models.Model):
    _name = "vn.report.line"
    _description = "TT99 financial statement line"
    _order = "report_id, sequence, id"

    report_id = fields.Many2one("vn.report", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer(default=10)
    code = fields.Char(string="Mã số", required=True)
    name = fields.Char(string="Chỉ tiêu", required=True, translate=True)
    note_ref = fields.Char(string="Thuyết minh")
    formula = fields.Char(
        string="Công thức",
        help="Ví dụ: PDR(1311) — dư Nợ chi tiết theo đối tượng của TK 1311; "
        "[110] + [120] — cộng các mã số khác. Để trống nếu chỉ tiêu nhập tay.",
    )
    level = fields.Integer(default=2, help="0: phần lớn, 1: mục La Mã, 2: chỉ tiêu, 3: dòng chi tiết")
    bold = fields.Boolean()
    hide_if_zero = fields.Boolean(
        string="Ẩn khi bằng 0",
        help="TT99 cho phép không trình bày chỉ tiêu không có số liệu.",
    )

    _code_uniq = models.Constraint(
        "UNIQUE(report_id, code)", "Mã số phải là duy nhất trong một báo cáo."
    )

    @api.constrains("formula")
    def _check_formula(self):
        for line in self:
            try:
                parse_formula(line.formula)
            except FormulaError as e:
                raise ValidationError(f"[{line.code}] {e}") from e

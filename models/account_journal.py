from odoo import fields, models


class AccountJournal(models.Model):
    _inherit = "account.journal"

    vn_ledger = fields.Selection(
        [
            ("all", "VAS and IFRS"),
            ("vas", "VAS Only"),
            ("ifrs", "IFRS Adjustments Only"),
        ],
        string="Ledger",
        default="all",
        required=True,
        help="VAS is the statutory books. Entries of an 'IFRS Adjustments Only' journal are added to the VAS"
        " figures in IFRS reports and left out of VAS reports; entries of a 'VAS Only' journal are left"
        " out of IFRS reports.",
    )

{  # noqa: B018
    "name": "Vietnam - TT99/2025 Financial Statements",
    "version": "19.0.1.0.0",
    "summary": "Báo cáo tài chính theo Thông tư 99/2025/TT-BTC (B01, B02, B03-DN)",
    "description": """
Financial statements under Circular 99/2025/TT-BTC, built on the core
Vietnamese chart of accounts (l10n_vn), which already follows the TT99 list.

- B01-DN Báo cáo tình hình tài chính
- B02-DN Báo cáo kết quả hoạt động kinh doanh
- B03-DN Báo cáo lưu chuyển tiền tệ (trực tiếp và gián tiếp)

Every line ("mã số") has an editable formula, so a company can adapt the
mapping to its own detail accounts. PDF and XLSX output.
""",
    "category": "Accounting/Localizations/Reporting",
    "author": "Tricasia",
    "license": "LGPL-3",
    "depends": ["account", "l10n_vn"],
    "external_dependencies": {"python": ["xlsxwriter"]},
    "data": [
        "security/ir.model.access.csv",
        "data/vn_report_b01.xml",
        "data/vn_report_b02.xml",
        "data/vn_report_b03.xml",
        "report/vn_report_templates.xml",
        "views/vn_report_views.xml",
        "wizard/vn_report_wizard_views.xml",
        "views/menus.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "l10n_vn_tt99_reports/static/src/dynamic_report/*",
        ],
    },
    "installable": True,
}

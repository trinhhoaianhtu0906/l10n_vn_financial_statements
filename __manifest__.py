{  # noqa: B018
    "name": "Vietnam - Financial Statements (Circular 99/2025)",
    "version": "19.0.1.0.0",
    "summary": "Financial statements under Circular 99/2025/TT-BTC (B01, B02, B03-DN)",
    "description": """
Financial statements under Circular 99/2025/TT-BTC, built on the core
Vietnamese chart of accounts (l10n_vn), which already follows the Circular 99 list.

- B01-DN Statement of financial position
- B02-DN Income statement
- B03-DN Cash flow statement (direct and indirect methods)

Every line has an editable formula, so a company can adapt the
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
            "l10n_vn_financial_statements/static/src/dynamic_report/*",
        ],
    },
    "installable": True,
}

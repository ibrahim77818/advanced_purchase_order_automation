# -*- coding: utf-8 -*-
{
    "name": "Purchase Order Automation",
    "version": "18.0.1.0.0",
    "summary": "One-click procurement: confirm PO, receive goods, create / post / pay "
               "vendor bills — on confirmation or as a batch action.",
    "description": """
Purchase Order Automation
=========================
Automates the procure-to-pay chain from a single confirmation or a mass action:

* Scenario A: Confirm PO -> Validate Receipt -> Draft Vendor Bill
* Scenario B: Confirm PO -> Validate Receipt -> Posted Vendor Bill
* Scenario C: Confirm PO -> Validate Receipt -> Posted Bill -> Registered Payment

Features: per-company settings, manager-only restriction, batch wizard with
per-run overrides (scenario, warehouse, bill journal, payment journal, dates),
record-level savepoints with chatter logging of failures, and unit tests.
Works on Odoo 18 Community and Enterprise.
""",
    "category": "Inventory/Purchase",
    "author": "Ibrahim Yahia",
    "website": "https://www.linkedin.com/in/ibrahim-yahia",
    "license": "LGPL-3",
    "depends": ["purchase_stock", "account"],
    "data": [
        "security/ir.model.access.csv",
        "wizard/purchase_automation_wizard_views.xml",
        "data/ir_actions_server.xml",
        "views/res_config_settings_views.xml",
        "views/purchase_order_views.xml",
    ],
    "images": ["static/description/banner.png"],
    "installable": True,
    "application": False,
    "auto_install": False,
}

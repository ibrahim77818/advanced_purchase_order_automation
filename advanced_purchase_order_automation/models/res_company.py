# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

POA_SCENARIOS = [
    ("bill_draft", "A - Confirm, Receive, Draft Bill"),
    ("bill_posted", "B - Confirm, Receive, Posted Bill"),
    ("bill_paid", "C - Confirm, Receive, Posted Bill, Payment"),
]

POA_BILL_DATE_SOURCES = [
    ("today", "Today"),
    ("receipt", "Receipt Validation Date"),
    ("order", "Order Date"),
]

POA_BACKORDER_POLICIES = [
    ("never", "Never create backorders (cancel remainders)"),
    ("always", "Always create backorders for remainders"),
]

POA_ERROR_POLICIES = [
    ("log", "Keep the PO confirmed and log the failure in the chatter"),
    ("raise", "Block the confirmation and show the error"),
]


class ResCompany(models.Model):
    _inherit = "res.company"

    poa_enabled = fields.Boolean(
        string="Purchase Automation",
        help="Master switch for procurement automation in this company.",
    )
    poa_trigger_on_confirm = fields.Boolean(
        string="Run on PO Confirmation",
        default=True,
        help="Run the automation automatically when a purchase order is "
             "confirmed (or approved, when double validation applies).",
    )
    poa_restrict_to_managers = fields.Boolean(
        string="Restrict to Purchase Managers",
        help="Only members of Purchase / Administrator may trigger the "
             "automation. Other users confirm orders the standard way.",
    )
    poa_scenario = fields.Selection(
        POA_SCENARIOS, string="Automation Scenario", default="bill_draft",
    )
    poa_bill_journal_id = fields.Many2one(
        "account.journal", string="Vendor Bill Journal",
        domain="[('type', '=', 'purchase'), ('company_id', '=', id)]",
        help="Leave empty to use the default purchase journal.",
    )
    poa_payment_journal_id = fields.Many2one(
        "account.journal", string="Payment Journal",
        domain="[('type', 'in', ('bank', 'cash')), ('company_id', '=', id)]",
        help="Bank or cash journal used to pay bills in scenario C.",
    )
    poa_payment_method_line_id = fields.Many2one(
        "account.payment.method.line", string="Payment Method",
        domain="[('journal_id', '=', poa_payment_journal_id), ('payment_type', '=', 'outbound')]",
        help="Optional outgoing payment method. Defaults to the journal's first one.",
    )
    poa_bill_date_source = fields.Selection(
        POA_BILL_DATE_SOURCES, string="Bill Date", default="today",
    )
    poa_backorder_policy = fields.Selection(
        POA_BACKORDER_POLICIES, string="Backorders", default="never",
    )
    poa_on_confirm_error = fields.Selection(
        POA_ERROR_POLICIES, string="On Confirmation Error", default="log",
    )

    @api.constrains("poa_payment_journal_id", "poa_payment_method_line_id")
    def _check_poa_payment_config(self):
        for company in self:
            journal = company.poa_payment_journal_id
            if journal and journal.type not in ("bank", "cash"):
                raise ValidationError(_("The automation payment journal must be a bank or cash journal."))
            method = company.poa_payment_method_line_id
            if method and method.journal_id != journal:
                raise ValidationError(_("The automation payment method must belong to the payment journal."))

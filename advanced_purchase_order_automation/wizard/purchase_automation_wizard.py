# -*- coding: utf-8 -*-
from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..models.res_company import POA_BACKORDER_POLICIES, POA_SCENARIOS


class PurchaseAutomationWizard(models.TransientModel):
    _name = "purchase.automation.wizard"
    _description = "Purchase Order Automation - Batch Run"

    purchase_ids = fields.Many2many(
        "purchase.order", string="Purchase Orders", required=True,
        default=lambda self: self._default_purchase_ids(),
    )
    order_count = fields.Integer(compute="_compute_company")
    company_id = fields.Many2one("res.company", compute="_compute_company")
    multi_company = fields.Boolean(compute="_compute_company")

    scenario = fields.Selection(POA_SCENARIOS, required=True,
                                default=lambda self: self._default_company().poa_scenario or "bill_draft")
    warehouse_id = fields.Many2one(
        "stock.warehouse", string="Receive In Warehouse",
        domain="[('company_id', '=', company_id)]",
        help="Applies to orders still in RFQ state: their receipt is created in this warehouse. "
             "Leave empty to keep each order's operation type.",
    )
    bill_journal_id = fields.Many2one(
        "account.journal", string="Bill Journal",
        domain="[('type', '=', 'purchase'), ('company_id', '=', company_id)]",
        help="Leave empty to use the company setting / default purchase journal.",
    )
    payment_journal_id = fields.Many2one(
        "account.journal", string="Payment Journal",
        domain="[('type', 'in', ('bank', 'cash')), ('company_id', '=', company_id)]",
        help="Leave empty to use the company's automation payment journal.",
    )
    payment_method_line_id = fields.Many2one(
        "account.payment.method.line", string="Payment Method",
        domain="[('journal_id', '=', payment_journal_id), ('payment_type', '=', 'outbound')]",
    )
    bill_date = fields.Date(string="Bill Date", help="Leave empty to follow the company rule.")
    payment_date = fields.Date(string="Payment Date", help="Leave empty to use the bill date.")
    backorder_policy = fields.Selection(
        POA_BACKORDER_POLICIES, string="Backorders", required=True,
        default=lambda self: self._default_company().poa_backorder_policy or "never",
    )
    auto_lots = fields.Boolean(
        string="Generate Lot/Serial Numbers",
        help="Generate lot / serial numbers (<PO>-<product code>-0001...) for tracked "
             "products that have none on the receipt.",
    )

    state = fields.Selection([("draft", "Draft"), ("done", "Done")], default="draft")
    processed_count = fields.Integer(readonly=True)
    skipped_count = fields.Integer(readonly=True)
    failed_count = fields.Integer(readonly=True)
    failed_ids = fields.Many2many("purchase.order", "purchase_automation_wizard_failed_rel",
                                  string="Failed Orders", readonly=True)
    result_html = fields.Html(readonly=True, sanitize=True)

    # ------------------------------------------------------------------
    @api.model
    def _default_purchase_ids(self):
        ctx = self.env.context
        if ctx.get("active_model") == "purchase.order" and ctx.get("active_ids"):
            return [(6, 0, ctx["active_ids"])]
        return []

    @api.model
    def _default_company(self):
        ctx = self.env.context
        if ctx.get("active_model") == "purchase.order" and ctx.get("active_ids"):
            companies = self.env["purchase.order"].browse(ctx["active_ids"]).company_id
            if len(companies) == 1:
                return companies
        return self.env.company

    @api.depends("purchase_ids")
    def _compute_company(self):
        for wizard in self:
            companies = wizard.purchase_ids.company_id
            wizard.order_count = len(wizard.purchase_ids)
            wizard.multi_company = len(companies) > 1
            wizard.company_id = companies if len(companies) == 1 else self.env.company

    @api.onchange("payment_journal_id")
    def _onchange_payment_journal_id(self):
        if self.payment_method_line_id.journal_id != self.payment_journal_id:
            self.payment_method_line_id = False

    # ------------------------------------------------------------------
    def _get_options(self):
        self.ensure_one()
        return {
            "scenario": self.scenario,
            "warehouse": self.warehouse_id,
            "bill_journal": self.bill_journal_id,
            "payment_journal": self.payment_journal_id,
            "payment_method_line": self.payment_method_line_id,
            "bill_date": self.bill_date,
            "payment_date": self.payment_date,
            "backorder_policy": self.backorder_policy,
            "auto_lots": self.auto_lots,
        }

    def _check_overrides(self):
        self.ensure_one()
        if not self.purchase_ids:
            raise UserError(_("Select at least one purchase order."))
        has_overrides = bool(self.warehouse_id or self.bill_journal_id or self.payment_journal_id)
        if self.multi_company and has_overrides:
            raise UserError(_(
                "Warehouse and journal overrides need all selected orders to belong to the same "
                "company. Run the companies separately or clear the overrides."))
        wrong = (self.warehouse_id.company_id | self.bill_journal_id.company_id
                 | self.payment_journal_id.company_id) - self.purchase_ids.company_id
        if has_overrides and wrong:
            raise UserError(_("The selected warehouse/journals belong to another company."))

    def action_run(self):
        self.ensure_one()
        self._check_overrides()
        orders = self.purchase_ids
        orders._poa_check_access()
        result = orders._poa_run_batch(self._get_options())
        self.write({
            "state": "done",
            "processed_count": len(result["processed"]),
            "skipped_count": len(result["skipped"]),
            "failed_count": len(result["failed"]),
            "failed_ids": [(6, 0, result["failed"].ids)],
            "result_html": self._render_result(result),
        })
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
            "name": _("Purchase Automation - Result"),
        }

    def _render_result(self, result):
        if not result["failed"]:
            return Markup("<p>%s</p>") % _("All orders were processed without errors.")
        rows = Markup("").join(
            Markup("<tr><td>%s</td><td>%s</td></tr>") % (order.name, escape(result["errors"].get(order.id, "")))
            for order in result["failed"]
        )
        return Markup(
            "<table class='table table-sm'><thead><tr><th>%s</th><th>%s</th></tr></thead>"
            "<tbody>%s</tbody></table>"
        ) % (_("Order"), _("Error"), rows)

    def action_view_failed(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Failed Orders"),
            "res_model": "purchase.order",
            "view_mode": "list,form",
            "domain": [("id", "in", self.failed_ids.ids)],
        }

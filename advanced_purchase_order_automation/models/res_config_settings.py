# -*- coding: utf-8 -*-
from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    poa_enabled = fields.Boolean(related="company_id.poa_enabled", readonly=False)
    poa_trigger_on_confirm = fields.Boolean(related="company_id.poa_trigger_on_confirm", readonly=False)
    poa_restrict_to_managers = fields.Boolean(related="company_id.poa_restrict_to_managers", readonly=False)
    poa_scenario = fields.Selection(related="company_id.poa_scenario", readonly=False)
    poa_bill_journal_id = fields.Many2one(related="company_id.poa_bill_journal_id", readonly=False)
    poa_payment_journal_id = fields.Many2one(related="company_id.poa_payment_journal_id", readonly=False)
    poa_payment_method_line_id = fields.Many2one(
        related="company_id.poa_payment_method_line_id", readonly=False)
    poa_bill_date_source = fields.Selection(related="company_id.poa_bill_date_source", readonly=False)
    poa_backorder_policy = fields.Selection(related="company_id.poa_backorder_policy", readonly=False)
    poa_on_confirm_error = fields.Selection(related="company_id.poa_on_confirm_error", readonly=False)

    @api.onchange("poa_payment_journal_id")
    def _onchange_poa_payment_journal_id(self):
        if self.poa_payment_method_line_id.journal_id != self.poa_payment_journal_id:
            self.poa_payment_method_line_id = False

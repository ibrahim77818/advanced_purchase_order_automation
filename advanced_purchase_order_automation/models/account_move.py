# -*- coding: utf-8 -*-
from odoo import fields, models


class AccountMove(models.Model):
    _inherit = "account.move"

    poa_generated = fields.Boolean(
        string="Created by Purchase Automation",
        readonly=True,
        copy=False,
        help="Technical flag: this vendor bill was created by the purchase automation. "
             "Only flagged bills are posted / paid automatically when a run is resumed.",
    )

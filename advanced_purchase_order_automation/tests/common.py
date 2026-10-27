# -*- coding: utf-8 -*-
from odoo import Command
from odoo.addons.account.tests.common import AccountTestInvoicingCommon


class PurchaseAutomationCommon(AccountTestInvoicingCommon):
    """Shared fixtures.

    ``AccountTestInvoicingCommon`` builds two fresh companies with the generic
    chart of accounts, journals and taxes. It exists unchanged in Community
    and Enterprise, so the suite runs on both editions.
    """

    @classmethod
    def setUpClass(cls):
        # v18: the chart template is chosen with @setup_chart_template, not an argument.
        super().setUpClass()
        cls.company = cls.company_data["company"]
        cls.purchase_journal = cls.company_data["default_journal_purchase"]
        cls.bank_journal = cls.company_data["default_journal_bank"]
        cls.cash_journal = cls.company_data["default_journal_cash"]
        cls.warehouse = cls.env["stock.warehouse"].search([("company_id", "=", cls.company.id)], limit=1)

        cls.company.write({
            "poa_enabled": True,
            "poa_trigger_on_confirm": True,
            "poa_scenario": "bill_draft",
            "poa_payment_journal_id": cls.bank_journal.id,
            "poa_on_confirm_error": "log",
        })

        cls.vendor = cls.env["res.partner"].create({"name": "POA Vendor", "company_id": False})
        product_vals = {
            "uom_id": cls.env.ref("uom.product_uom_unit").id,
            "supplier_taxes_id": [Command.set(cls.tax_purchase_a.ids)],
            "purchase_method": "receive",
        }
        cls.product_storable = cls.env["product.product"].create(dict(
            product_vals, name="POA Storable", default_code="POA-STO", type="consu", is_storable=True,
            standard_price=10.0))
        cls.product_consu = cls.env["product.product"].create(dict(
            product_vals, name="POA Consumable", default_code="POA-CON", type="consu",
            purchase_method="purchase"))
        cls.product_service = cls.env["product.product"].create(dict(
            product_vals, name="POA Service", default_code="POA-SRV", type="service"))
        cls.product_serial = cls.env["product.product"].create(dict(
            product_vals, name="POA Serial", default_code="POA-SN", type="consu", is_storable=True,
            tracking="serial"))
        cls.product_lot = cls.env["product.product"].create(dict(
            product_vals, name="POA Lot", default_code="POA-LOT", type="consu", is_storable=True,
            tracking="lot"))

    @classmethod
    def _create_po(cls, lines=None, company=None, partner=None):
        company = company or cls.company
        if lines is None:
            lines = [(cls.product_storable, 5.0, 20.0), (cls.product_consu, 2.0, 7.5)]
        po = cls.env["purchase.order"].with_company(company).create({
            "partner_id": (partner or cls.vendor).id,
            "company_id": company.id,
            "order_line": [
                Command.create({
                    "product_id": product.id,
                    "product_qty": qty,
                    "price_unit": price,
                    "tax_ids": [Command.set(product.supplier_taxes_id.filtered(
                        lambda t: t.company_id == company).ids)],
                })
                for product, qty, price in lines
            ],
        })
        return po

    # ------------------------------------------------------------------
    def _receipts(self, po):
        return po.picking_ids.filtered(lambda p: p.picking_type_code == "incoming")

    def assertReceived(self, po):
        receipts = self._receipts(po)
        self.assertTrue(receipts, "The order should have a receipt")
        self.assertTrue(all(p.state == "done" for p in receipts), "All receipts should be done")
        for line in po.order_line.filtered(lambda l: l.product_id.type == "consu"):
            self.assertEqual(line.qty_received, line.product_qty)

    def assertBill(self, po, state):
        bills = po.invoice_ids
        self.assertEqual(len(bills), 1, "Exactly one vendor bill expected")
        self.assertEqual(bills.move_type, "in_invoice")
        self.assertEqual(bills.state, state)
        self.assertTrue(bills.poa_generated)
        self.assertTrue(bills.invoice_date)
        self.assertAlmostEqual(bills.amount_total, po.amount_total)
        return bills

    def _poa_messages(self, po):
        return po.message_ids.filtered(lambda m: "Purchase automation" in (m.body or ""))

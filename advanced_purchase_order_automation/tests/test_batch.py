# -*- coding: utf-8 -*-
from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import Form, tagged

from .common import PurchaseAutomationCommon


@tagged("post_install", "-at_install", "advanced_purchase_order_automation")
class TestPurchaseAutomationBatch(PurchaseAutomationCommon):
    """Mass-action channel: wizard, server action, isolation and overrides."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # The batch channel must work even when the confirm hook is off.
        cls.company.poa_trigger_on_confirm = False

    def _wizard(self, orders, **values):
        ctx = {"active_model": "purchase.order", "active_ids": orders.ids}
        form = Form(self.env["purchase.automation.wizard"].with_context(**ctx))
        for key, value in values.items():
            setattr(form, key, value)
        return form.save()

    # ------------------------------------------------------------------
    def test_wizard_defaults_from_company(self):
        self.company.write({"poa_scenario": "bill_posted", "poa_backorder_policy": "always"})
        orders = self._create_po() | self._create_po()
        wizard = self._wizard(orders)
        self.assertEqual(wizard.purchase_ids, orders)
        self.assertEqual(wizard.order_count, 2)
        self.assertEqual(wizard.company_id, self.company)
        self.assertEqual(wizard.scenario, "bill_posted")
        self.assertEqual(wizard.backorder_policy, "always")

    def test_wizard_all_scenarios_in_batch(self):
        for scenario, bill_state in (("bill_draft", "draft"), ("bill_posted", "posted"), ("bill_paid", "posted")):
            with self.subTest(scenario=scenario):
                orders = self._create_po() | self._create_po()
                orders[1].write({"state": "sent"})  # an RFQ already sent to the vendor
                wizard = self._wizard(orders, scenario=scenario)
                wizard.action_run()
                self.assertEqual(wizard.state, "done")
                self.assertEqual(wizard.processed_count, 2)
                self.assertEqual(wizard.failed_count, 0)
                for po in orders:
                    self.assertEqual(po.state, "purchase")
                    self.assertReceived(po)
                    bill = self.assertBill(po, bill_state)
                    if scenario == "bill_paid":
                        self.assertIn(bill.payment_state, ("paid", "in_payment"))

    def test_error_isolation_in_batch(self):
        """One broken order must not stop the batch nor leave partial data."""
        good_1 = self._create_po()
        broken = self._create_po([(self.product_serial, 3.0, 30.0)])
        good_2 = self._create_po()
        orders = good_1 | broken | good_2

        wizard = self._wizard(orders, scenario="bill_posted")
        wizard.action_run()

        self.assertEqual(wizard.processed_count, 2)
        self.assertEqual(wizard.failed_count, 1)
        self.assertEqual(wizard.failed_ids, broken)
        self.assertIn(broken.name, wizard.result_html)

        for po in good_1 | good_2:
            self.assertReceived(po)
            self.assertBill(po, "posted")
            self.assertEqual(po.poa_state, "done")

        # Everything done on the broken order was rolled back to its savepoint ...
        self.assertEqual(broken.state, "draft")
        self.assertFalse(broken.picking_ids)
        self.assertFalse(broken.invoice_ids)
        # ... but the failure is documented.
        self.assertEqual(broken.poa_state, "failed")
        self.assertIn("Receipt Validation", broken.poa_last_error)
        self.assertTrue(broken.message_ids.filtered(lambda m: "automation failed" in (m.body or "")))
        failed_action = wizard.action_view_failed()
        self.assertEqual(failed_action["domain"], [("id", "in", broken.ids)])

    def test_server_action_uses_company_defaults(self):
        self.company.poa_scenario = "bill_posted"
        orders = self._create_po() | self._create_po([(self.product_lot, 1.0, 10.0)])
        action = self.env.ref("advanced_purchase_order_automation.action_server_purchase_automation_run")
        result = action.with_context(active_model="purchase.order", active_ids=orders.ids,
                                     active_id=orders[0].id).run()
        self.assertEqual(result["tag"], "display_notification")
        self.assertEqual(result["params"]["type"], "warning")
        self.assertIn("1 processed", result["params"]["message"])
        self.assertIn("1 failed", result["params"]["message"])
        self.assertBill(orders[0], "posted")
        self.assertEqual(orders[1].poa_state, "failed")

    def test_wizard_overrides(self):
        """Per-run overrides: warehouse, bill journal, payment journal and dates."""
        self.company.poa_scenario = "bill_draft"
        warehouse_2 = self.env["stock.warehouse"].create({
            "name": "POA Second Warehouse", "code": "POA2", "company_id": self.company.id,
        })
        journal_2 = self.purchase_journal.copy({"name": "POA Batch Purchases", "code": "POAB"})
        bill_date = fields.Date.to_date("2026-09-15")
        payment_date = fields.Date.to_date("2026-09-20")
        po = self._create_po()

        wizard = self._wizard(
            po, scenario="bill_paid", warehouse_id=warehouse_2, bill_journal_id=journal_2,
            payment_journal_id=self.cash_journal, bill_date=bill_date, payment_date=payment_date)
        wizard.action_run()

        self.assertEqual(wizard.processed_count, 1)
        self.assertEqual(po.picking_type_id, warehouse_2.in_type_id)
        self.assertEqual(self._receipts(po).picking_type_id.warehouse_id, warehouse_2)
        self.assertReceived(po)
        bill = self.assertBill(po, "posted")
        self.assertEqual(bill.journal_id, journal_2)
        self.assertEqual(bill.invoice_date, bill_date)
        payment = bill._get_reconciled_payments()
        self.assertEqual(payment.journal_id, self.cash_journal)
        self.assertEqual(payment.date, payment_date)
        # Company defaults are untouched by the run.
        self.assertEqual(self.company.poa_scenario, "bill_draft")

    def test_auto_generated_lots_and_serials(self):
        po = self._create_po([(self.product_serial, 3.0, 30.0), (self.product_lot, 4.0, 5.0)])
        wizard = self._wizard(po, scenario="bill_posted", auto_lots=True)
        wizard.action_run()

        self.assertEqual(wizard.failed_count, 0, wizard.result_html)
        self.assertReceived(po)
        move_lines = self._receipts(po).move_line_ids
        serials = move_lines.filtered(lambda ml: ml.product_id == self.product_serial).lot_id
        lots = move_lines.filtered(lambda ml: ml.product_id == self.product_lot).lot_id
        self.assertEqual(len(serials), 3)
        self.assertEqual(len(set(serials.mapped("name"))), 3)
        self.assertEqual(len(lots), 1)
        self.assertTrue(all(name.startswith(po.name.replace("/", "-")) for name in (serials | lots).mapped("name")))
        self.assertBill(po, "posted")

    def test_resume_partially_processed_order(self):
        """A run in scenario A then a run in scenario C completes the same bill."""
        po = self._create_po()
        self._wizard(po, scenario="bill_draft").action_run()
        bill = self.assertBill(po, "draft")

        wizard = self._wizard(po, scenario="bill_paid")
        wizard.action_run()
        self.assertEqual(wizard.processed_count, 1)
        self.assertEqual(po.invoice_ids, bill, "No second bill may be created")
        self.assertEqual(bill.state, "posted")
        self.assertIn(bill.payment_state, ("paid", "in_payment"))

        # Third run: nothing left to do.
        wizard = self._wizard(po, scenario="bill_paid")
        wizard.action_run()
        self.assertEqual(wizard.skipped_count, 1)
        self.assertEqual(wizard.processed_count, 0)

    def test_manual_bills_are_not_posted(self):
        """Bills created by hand are never posted by the automation."""
        po = self._create_po()
        po.button_confirm()
        receipt = self._receipts(po)
        receipt.button_validate()  # v17 validates the reserved quantities directly
        po.action_create_invoice()
        manual_bill = po.invoice_ids
        manual_bill.invoice_date = fields.Date.today()

        wizard = self._wizard(po, scenario="bill_paid")
        wizard.action_run()
        self.assertEqual(manual_bill.state, "draft")
        self.assertEqual(wizard.skipped_count, 1)

    def test_existing_backorder_is_received(self):
        """A receipt partially processed by hand leaves a backorder; the batch
        validates the backorder and bills the full quantity."""
        po = self._create_po([(self.product_storable, 10.0, 3.0)])
        po.button_confirm()
        receipt = self._receipts(po)
        receipt.move_ids.quantity = 4.0
        receipt.move_ids.picked = True
        res = receipt.button_validate()
        Form(self.env[res["res_model"]].with_context(res["context"])).save().process()
        backorder = self._receipts(po).filtered(lambda p: p.state != "done")
        self.assertTrue(backorder)

        wizard = self._wizard(po, scenario="bill_posted")
        wizard.action_run()
        self.assertEqual(wizard.failed_count, 0, wizard.result_html)
        self.assertEqual(backorder.state, "done")
        self.assertEqual(po.order_line.qty_received, 10.0)
        self.assertEqual(po.order_line.qty_invoiced, 10.0)

    def test_cancelled_orders_are_skipped(self):
        po = self._create_po()
        po.button_cancel()
        wizard = self._wizard(po)
        wizard.action_run()
        self.assertEqual(wizard.skipped_count, 1)
        self.assertEqual(po.state, "cancel")
        self.assertFalse(po.poa_state)

    def test_disabled_company_refuses_mass_action(self):
        self.company.poa_enabled = False
        po = self._create_po()
        with self.assertRaises(UserError):
            self._wizard(po).action_run()
        self.assertEqual(po.state, "draft")

    def test_multi_company_batch(self):
        # v18: the second company is created on demand.
        company_data_2 = self.setup_other_company()
        company_2 = company_data_2["company"]
        company_2.write({"poa_enabled": True, "poa_scenario": "bill_posted",
                         "poa_trigger_on_confirm": False})
        self.company.poa_scenario = "bill_draft"
        po_1 = self._create_po()
        po_2 = self._create_po(company=company_2)
        orders = po_1 | po_2

        wizard = self.env["purchase.automation.wizard"].with_context(
            active_model="purchase.order", active_ids=orders.ids,
            allowed_company_ids=(self.company | company_2).ids,
        ).create({})
        self.assertTrue(wizard.multi_company)

        # Journal overrides are refused across companies ...
        wizard.bill_journal_id = self.purchase_journal
        with self.assertRaises(UserError):
            wizard.action_run()
        # ... and without them each order follows the wizard scenario in its own company.
        wizard.bill_journal_id = False
        wizard.scenario = "bill_posted"
        wizard.action_run()
        self.assertEqual(wizard.processed_count, 2, wizard.result_html)
        self.assertEqual(po_1.invoice_ids.company_id, self.company)
        self.assertEqual(po_2.invoice_ids.company_id, company_2)
        self.assertEqual(po_2.invoice_ids.journal_id, company_data_2["default_journal_purchase"])
        self.assertTrue(all(b.state == "posted" for b in orders.invoice_ids))

    def test_open_wizard_action(self):
        orders = self._create_po() | self._create_po()
        action = orders.action_poa_open_wizard()
        self.assertEqual(action["res_model"], "purchase.automation.wizard")
        self.assertEqual(action["context"]["active_ids"], orders.ids)

# -*- coding: utf-8 -*-
from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import Form, tagged

from .common import PurchaseAutomationCommon


@tagged("post_install", "-at_install", "advanced_purchase_order_automation")
class TestPurchaseAutomationScenarios(PurchaseAutomationCommon):
    """Confirmation-button channel: the three scenarios and the global switches."""

    # ------------------------------------------------------------------
    # Scenario A / B / C
    # ------------------------------------------------------------------
    def test_scenario_a_draft_bill(self):
        self.company.poa_scenario = "bill_draft"
        po = self._create_po()
        self.assertEqual(po.state, "draft")

        po.button_confirm()

        self.assertEqual(po.state, "purchase")
        self.assertReceived(po)
        bill = self.assertBill(po, "draft")
        self.assertEqual(bill.journal_id, self.purchase_journal)
        self.assertEqual(bill.invoice_date, fields.Date.context_today(po))
        self.assertEqual(po.invoice_status, "invoiced")
        self.assertEqual(po.poa_state, "done")
        self.assertTrue(self._poa_messages(po), "A success note is expected in the chatter")

    def test_scenario_b_posted_bill(self):
        self.company.poa_scenario = "bill_posted"
        po = self._create_po()
        po.button_confirm()

        self.assertEqual(po.state, "purchase")
        self.assertReceived(po)
        bill = self.assertBill(po, "posted")
        self.assertEqual(bill.payment_state, "not_paid")
        self.assertTrue(bill.name and bill.name != "/")

    def test_scenario_c_paid_bill(self):
        self.company.poa_scenario = "bill_paid"
        po = self._create_po()
        po.button_confirm()

        self.assertEqual(po.state, "purchase")
        self.assertReceived(po)
        bill = self.assertBill(po, "posted")
        # 'in_payment' when the outstanding account is not yet bank-reconciled (Enterprise
        # accounting), 'paid' otherwise.
        self.assertIn(bill.payment_state, ("paid", "in_payment"))
        payments = bill._get_reconciled_payments()
        self.assertEqual(len(payments), 1)
        self.assertEqual(payments.journal_id, self.bank_journal)
        self.assertEqual(payments.payment_type, "outbound")
        self.assertAlmostEqual(payments.amount, bill.amount_total)
        # v18: payment states are draft / in_process / paid (no "posted").
        self.assertIn(payments.state, ("in_process", "paid"))

    def test_scenario_c_with_payment_method(self):
        method = self.bank_journal.outbound_payment_method_line_ids[:1]
        self.company.write({"poa_scenario": "bill_paid", "poa_payment_method_line_id": method.id})
        po = self._create_po()
        po.button_confirm()
        payments = po.invoice_ids._get_reconciled_payments()
        self.assertEqual(payments.payment_method_line_id, method)

    def test_scenario_c_cash_journal(self):
        self.company.write({"poa_scenario": "bill_paid", "poa_payment_journal_id": self.cash_journal.id})
        po = self._create_po()
        po.button_confirm()
        payments = po.invoice_ids._get_reconciled_payments()
        self.assertEqual(payments.journal_id, self.cash_journal)

    # ------------------------------------------------------------------
    # Configuration switches
    # ------------------------------------------------------------------
    def test_settings_form_writes_company(self):
        settings = Form(self.env["res.config.settings"].with_company(self.company))
        settings.poa_scenario = "bill_posted"
        settings.poa_restrict_to_managers = True
        settings.poa_bill_date_source = "receipt"
        settings.save().execute()
        self.assertEqual(self.company.poa_scenario, "bill_posted")
        self.assertTrue(self.company.poa_restrict_to_managers)
        self.assertEqual(self.company.poa_bill_date_source, "receipt")

    def test_disabled_globally(self):
        self.company.poa_enabled = False
        po = self._create_po()
        po.button_confirm()
        self.assertEqual(po.state, "purchase")
        self.assertTrue(all(p.state != "done" for p in self._receipts(po)))
        self.assertFalse(po.invoice_ids)
        self.assertFalse(po.poa_state)

    def test_trigger_on_confirm_off(self):
        self.company.poa_trigger_on_confirm = False
        po = self._create_po()
        po.button_confirm()
        self.assertTrue(all(p.state != "done" for p in self._receipts(po)))
        self.assertFalse(po.invoice_ids)

    def test_restricted_to_managers(self):
        self.company.poa_restrict_to_managers = True
        base_groups = [
            self.env.ref("purchase.group_purchase_user").id,
            self.env.ref("stock.group_stock_user").id,
            self.env.ref("account.group_account_invoice").id,
        ]
        user = self.env["res.users"].create({
            "name": "POA Purchase User",
            "login": "poa_purchase_user",
            "email": "poa.user@example.com",
            "company_id": self.company.id,
            "company_ids": [Command.set(self.company.ids)],
            "group_ids": [Command.set(base_groups)],
        })
        manager = self.env["res.users"].create({
            "name": "POA Purchase Manager",
            "login": "poa_purchase_manager",
            "email": "poa.manager@example.com",
            "company_id": self.company.id,
            "company_ids": [Command.set(self.company.ids)],
            "group_ids": [Command.set(base_groups + [self.env.ref("purchase.group_purchase_manager").id])],
        })

        po_user = self._create_po()
        po_user.with_user(user).button_confirm()
        self.assertEqual(po_user.state, "purchase")
        self.assertFalse(po_user.invoice_ids, "Non-managers must not trigger the automation")
        self.assertTrue(all(p.state != "done" for p in self._receipts(po_user)))

        po_manager = self._create_po()
        po_manager.with_user(manager).button_confirm()
        self.assertReceived(po_manager)
        self.assertBill(po_manager, "draft")

        # The mass action is refused for the non-manager.
        with self.assertRaises(UserError):
            po_user.with_user(user).action_poa_run_automation()

    def test_double_validation_runs_on_approval(self):
        self.company.write({
            "po_double_validation": "two_step",
            "po_double_validation_amount": 1.0,
            "poa_scenario": "bill_posted",
        })
        user = self.env["res.users"].create({
            "name": "POA Junior Buyer",
            "login": "poa_junior",
            "email": "poa.junior@example.com",
            "company_id": self.company.id,
            "company_ids": [Command.set(self.company.ids)],
            "group_ids": [Command.set([
                self.env.ref("purchase.group_purchase_user").id,
                self.env.ref("stock.group_stock_user").id,
            ])],
        })
        po = self._create_po()
        po.with_user(user).button_confirm()
        self.assertEqual(po.state, "to approve")
        self.assertFalse(po.picking_ids)

        po.button_approve()  # current test user is a purchase manager
        self.assertEqual(po.state, "purchase")
        self.assertReceived(po)
        self.assertBill(po, "posted")

    def test_bill_date_source_order(self):
        self.company.poa_bill_date_source = "order"
        po = self._create_po()
        po.date_order = fields.Datetime.to_datetime("2026-01-15 10:00:00")
        po.button_confirm()
        self.assertEqual(po.invoice_ids.invoice_date, fields.Date.to_date("2026-01-15"))

    def test_bill_journal_setting(self):
        other_journal = self.purchase_journal.copy({"name": "POA Purchases 2", "code": "POA2"})
        self.company.poa_bill_journal_id = other_journal
        po = self._create_po()
        po.button_confirm()
        self.assertEqual(po.invoice_ids.journal_id, other_journal)

    def test_service_lines_received_and_billed(self):
        self.company.poa_scenario = "bill_posted"
        po = self._create_po([(self.product_storable, 1.0, 50.0), (self.product_service, 3.0, 15.0)])
        po.button_confirm()
        service_line = po.order_line.filtered(lambda l: l.product_id == self.product_service)
        self.assertEqual(service_line.qty_received, 3.0)
        self.assertEqual(service_line.qty_invoiced, 3.0)
        self.assertBill(po, "posted")

    def test_service_only_order(self):
        self.company.poa_scenario = "bill_posted"
        po = self._create_po([(self.product_service, 2.0, 40.0)])
        po.button_confirm()
        self.assertFalse(po.picking_ids)
        self.assertBill(po, "posted")

    # ------------------------------------------------------------------
    # Failures on the confirmation channel
    # ------------------------------------------------------------------
    def test_confirm_error_logged_keeps_order_confirmed(self):
        """Default policy: the PO stays confirmed, downstream work is rolled back
        and the failure is written in the chatter."""
        self.company.poa_scenario = "bill_posted"
        po = self._create_po([(self.product_serial, 2.0, 100.0)])
        po.button_confirm()

        self.assertEqual(po.state, "purchase")
        receipt = self._receipts(po)
        self.assertTrue(receipt)
        self.assertNotEqual(receipt.state, "done")
        self.assertFalse(po.invoice_ids)
        self.assertEqual(po.poa_state, "failed")
        self.assertIn("Receipt Validation", po.poa_last_error)
        self.assertTrue(po.message_ids.filtered(lambda m: "automation failed" in (m.body or "")))

    def test_confirm_error_raise_policy(self):
        self.company.poa_on_confirm_error = "raise"
        po = self._create_po([(self.product_serial, 1.0, 100.0)])
        with self.assertRaises(UserError):
            po.button_confirm()
        po.invalidate_recordset()
        self.assertEqual(po.state, "draft")
        self.assertFalse(po.picking_ids)

    def test_missing_payment_journal_fails_cleanly(self):
        self.company.write({"poa_scenario": "bill_paid", "poa_payment_journal_id": False})
        po = self._create_po()
        po.button_confirm()
        # Confirmation stays; receipt/bill of this run were rolled back with the failed payment step.
        self.assertEqual(po.state, "purchase")
        self.assertFalse(po.invoice_ids)
        self.assertEqual(po.poa_state, "failed")
        self.assertIn("Payment Registration", po.poa_last_error)

    def test_fiscal_lock_date_respected(self):
        """A bill dated inside a locked period is posted with Odoo's standard
        lock-date handling (accounting date moved after the lock date)."""
        self.company.write({"poa_scenario": "bill_posted", "poa_bill_date_source": "order"})
        lock_date = fields.Date.to_date("2026-06-30")
        self.company.fiscalyear_lock_date = lock_date
        po = self._create_po()
        po.date_order = fields.Datetime.to_datetime("2026-03-01 09:00:00")
        po.button_confirm()
        bill = self.assertBill(po, "posted")
        self.assertEqual(bill.invoice_date, fields.Date.to_date("2026-03-01"))
        self.assertGreater(bill.date, lock_date)

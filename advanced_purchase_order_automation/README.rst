=========================
Purchase Order Automation
=========================

Odoo 19 (Community & Enterprise) module that runs the procure-to-pay chain in
one go: **confirm the PO -> validate the receipt -> create the vendor bill ->
post it -> pay it**, either when a buyer clicks *Confirm Order* or as a batch
action on any selection of RFQs / purchase orders.

Scenarios
=========

=====  ===================================================================
A      Confirm PO -> Validate receipt -> Draft vendor bill
B      Confirm PO -> Validate receipt -> Posted vendor bill
C      Confirm PO -> Validate receipt -> Posted bill -> Registered payment
=====  ===================================================================

Configuration
=============

*Purchase > Configuration > Settings > Procurement Automation* (per company):

* **Purchase Automation** - master switch.
* **Scenario** - A, B or C (default for both channels).
* **Run on PO Confirmation** - hook on confirmation/approval. With double
  validation the automation starts when the manager approves the order.
* **On Confirmation Error** - *log* (keep the PO confirmed, roll back the
  downstream steps, write the error in the chatter) or *raise* (block the
  confirmation with the error).
* **Restrict to Purchase Managers** - only ``purchase.group_purchase_manager``
  members trigger the automation; other users confirm normally and are refused
  by the batch actions.
* **Bill Date** - today, receipt validation date or order date.
* **Backorders** - cancel remainders (default) or keep them as backorders.
* **Vendor Bill Journal** (optional), **Payment Journal** and **Payment Method**
  (bank / cash, required for scenario C).

Usage
=====

1. **Confirm button** - with *Run on PO Confirmation* on, confirming an RFQ runs
   the company scenario.
2. **List view > Action > Run Procurement Automation (Company Defaults)** -
   server action, one click, company settings, result shown as a notification.
3. **List view > Action > Run Procurement Automation...** - wizard that overrides
   per run: scenario, receiving warehouse (orders still in RFQ state), bill
   journal, bill date, payment journal / method / date, backorder policy and
   automatic lot / serial number generation. It ends on a result screen
   (processed / nothing to do / failed, with the error of each failed order).

Orders get an *Automation Status* (Processed / Failed) with list badges,
*Automation Failed* search filter and a warning banner on the form.

Technical notes
===============

* Hooks ``purchase.order.button_approve`` (called by ``button_confirm`` for orders
  that need no second approval), so both one-step and two-step flows are covered.
  Internal calls pass ``poa_skip_auto`` to avoid recursion.
* Each order runs inside ``with self.env.cr.savepoint():``. On failure the
  savepoint is rolled back, the error is logged in the chatter
  (``_message_log``), ``poa_state`` is set to *failed* and the batch continues.
  Serialization failures / deadlocks are re-raised so Odoo can retry.
* Each order is row-locked (``FOR UPDATE NOWAIT``) so two users cannot automate
  the same order at once.
* Receipts: incoming pickings are confirmed and reserved, each move's ``quantity``
  is set to the demand and flagged ``picked``, then ``button_validate()`` is called
  with ``skip_backorder``, ``skip_sms`` and ``skip_expired``. If the picking is still
  not done (e.g. Enterprise quality checks), the order fails with a clear message.
* Service lines with manual received quantities are marked received so that
  "received quantities" billing control does not block the bill.
* Bills: native ``action_create_invoice()``; journal / date are injected through
  ``_prepare_invoice()``. Bills are flagged ``poa_generated``; only flagged bills
  are posted or paid, so bills created by hand are never touched.
* Payment: ``account.payment.register`` with ``active_ids`` = the bills, then
  ``action_create_payments()``; the bill must end ``paid`` or ``in_payment``.
* Idempotent: running again resumes (posts an earlier draft bill, pays an earlier
  posted bill) and reports *nothing to do* when the order is complete.

Tests
=====

``tests/`` contains 31 ``TransactionCase`` tests (based on
``AccountTestInvoicingCommon``, available in both editions) covering the three
scenarios, settings, manager restriction, double validation, bill date / journal
settings, service lines, error policies, batch isolation, server action, wizard
overrides, lot / serial generation, backorders, resume, cancelled orders and
multi-company batches::

    odoo-bin -d <db> -i advanced_purchase_order_automation --test-tags /advanced_purchase_order_automation --stop-after-init

Known limits
============

* Tracked products need lot / serial numbers on the receipt, or the wizard option
  *Generate Lot/Serial Numbers* (``<PO>-<product code>-0001`` ...).
* Receipts that need a manual step (quality checks, etc.) fail the order instead
  of being forced.
* Warehouse overrides only apply to orders not yet confirmed.

# -*- coding: utf-8 -*-
"""Procure-to-pay automation for purchase orders.

Pipeline (per order, inside its own savepoint):

    confirm  ->  receipt  ->  bill  ->  post (B, C)  ->  payment (C)

Every step is idempotent: re-running the automation on a partially processed
order resumes where it stopped (already-done receipts are skipped, draft bills
created by a previous run are posted, posted ones are paid).
"""
import logging
import re

import psycopg2
from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import float_compare, format_amount

from .res_company import POA_SCENARIOS

_logger = logging.getLogger(__name__)

STEP_CONFIRM = "confirm"
STEP_RECEIPT = "receipt"
STEP_BILL = "bill"
STEP_POST = "post"
STEP_PAYMENT = "payment"

SCENARIO_RANK = {"bill_draft": 1, "bill_posted": 2, "bill_paid": 3}


def _step_labels(env):
    # v18: use env._() here; a module-level function has no `self` from which the
    # plain _() could detect the user's language.
    return {
        STEP_CONFIRM: env._("Order Confirmation"),
        STEP_RECEIPT: env._("Receipt Validation"),
        STEP_BILL: env._("Vendor Bill Creation"),
        STEP_POST: env._("Vendor Bill Posting"),
        STEP_PAYMENT: env._("Payment Registration"),
    }


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    poa_state = fields.Selection(
        [("done", "Processed"), ("failed", "Failed")],
        string="Automation Status", readonly=True, copy=False, index=True,
    )
    poa_last_error = fields.Text(string="Last Automation Error", readonly=True, copy=False)
    poa_last_run = fields.Datetime(string="Last Automation Run", readonly=True, copy=False)

    # ------------------------------------------------------------------
    # Native hooks
    # ------------------------------------------------------------------
    def button_approve(self, force=False):
        """Hook on approval: ``button_confirm`` calls ``button_approve`` for
        orders that need no second validation, and managers call it for the
        ones waiting in 'to approve'. Hooking here covers both paths."""
        res = super().button_approve(force=force)
        if not self.env.context.get("poa_skip_auto"):
            self._poa_run_on_confirm()
        return res

    def _prepare_invoice(self):
        vals = super()._prepare_invoice()
        ctx = self.env.context
        if ctx.get("poa_generated"):
            vals["poa_generated"] = True
        if ctx.get("poa_bill_journal_id"):
            vals["journal_id"] = ctx["poa_bill_journal_id"]
        if ctx.get("poa_invoice_date"):
            vals["invoice_date"] = ctx["poa_invoice_date"]
        return vals

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------
    def _poa_run_on_confirm(self):
        orders = self.filtered(
            lambda po: po.state == "purchase"
            and po.company_id.poa_enabled
            and po.company_id.poa_trigger_on_confirm
            and po._poa_user_allowed()
        )
        if not orders:
            return
        strict = orders.filtered(lambda po: po.company_id.poa_on_confirm_error == "raise")
        if strict:
            strict._poa_run_batch(raise_on_error=True)
        (orders - strict)._poa_run_batch()

    def action_poa_run_automation(self):
        """Server action entry point (list view, company defaults)."""
        self._poa_check_access()
        result = self._poa_run_batch()
        return self._poa_notification_action(result)

    def action_poa_open_wizard(self):
        self._poa_check_access()
        action = self.env["ir.actions.actions"]._for_xml_id(
            "advanced_purchase_order_automation.action_purchase_automation_wizard")
        action["context"] = {"active_model": "purchase.order", "active_ids": self.ids}
        return action

    # ------------------------------------------------------------------
    # Access
    # ------------------------------------------------------------------
    def _poa_user_allowed(self):
        self.ensure_one()
        if self.env.su or not self.company_id.poa_restrict_to_managers:
            return True
        return self.env.user.has_group("purchase.group_purchase_manager")

    def _poa_check_access(self):
        if not self.env.user.has_group("purchase.group_purchase_user") and not self.env.su:
            raise AccessError(_("Only purchase users can run the procurement automation."))
        disabled = self.filtered(lambda po: not po.company_id.poa_enabled)
        if disabled:
            raise UserError(_(
                "Purchase automation is disabled for company %(company)s. "
                "Enable it in Purchase > Configuration > Settings.",
                company=", ".join(disabled.company_id.mapped("name")),
            ))
        denied = self.filtered(lambda po: not po._poa_user_allowed())
        if denied:
            raise AccessError(_(
                "Purchase automation is restricted to Purchase Administrators for %(company)s.",
                company=", ".join(denied.company_id.mapped("name")),
            ))

    # ------------------------------------------------------------------
    # Options
    # ------------------------------------------------------------------
    def _poa_default_options(self):
        self.ensure_one()
        company = self.company_id
        return {
            "scenario": company.poa_scenario or "bill_draft",
            "warehouse": self.env["stock.warehouse"],
            "bill_journal": company.poa_bill_journal_id,
            "payment_journal": company.poa_payment_journal_id,
            "payment_method_line": company.poa_payment_method_line_id,
            "bill_date": False,
            "payment_date": False,
            "bill_date_source": company.poa_bill_date_source or "today",
            "backorder_policy": company.poa_backorder_policy or "never",
            "auto_lots": False,
        }

    def _poa_merge_options(self, overrides):
        opts = self._poa_default_options()
        for key, value in (overrides or {}).items():
            # Empty overrides (False / empty recordset / None) keep the company default,
            # except booleans that are explicitly part of the run definition.
            if key == "auto_lots" or value:
                opts[key] = value
        return opts

    # ------------------------------------------------------------------
    # Batch engine
    # ------------------------------------------------------------------
    def _poa_run_batch(self, options=None, raise_on_error=False):
        """Process every order in ``self`` in its own savepoint.

        :param options: dict of per-run overrides (see ``_poa_default_options``)
        :param raise_on_error: re-raise the first failure instead of logging it
        :return: dict with ``processed``, ``skipped``, ``failed`` recordsets and
                 an ``errors`` mapping {order_id: message}
        """
        Order = self.browse()
        result = {"processed": Order, "skipped": Order, "failed": Order, "errors": {}}
        labels = _step_labels(self.env)
        for order in self:
            order = order.with_company(order.company_id)
            opts = order._poa_merge_options(options)
            step_tracker = {"step": STEP_CONFIRM}
            try:
                with self.env.cr.savepoint():
                    report = order._poa_process_single(opts, step_tracker)
            except psycopg2.extensions.TransactionRollbackError:
                # serialization failure / deadlock: let the server retry the request
                raise
            except Exception as exc:  # noqa: BLE001 - isolation is the point here
                step = step_tracker["step"]
                message = self._poa_exception_message(exc)
                if raise_on_error:
                    if isinstance(exc, UserError):
                        raise UserError(_(
                            "Purchase automation failed for %(order)s during %(step)s:\n%(error)s",
                            order=order.name, step=labels.get(step, step), error=message,
                        )) from exc
                    raise
                _logger.warning(
                    "Purchase automation failed for %s (id %s) at step '%s': %s",
                    order.name, order.id, step, message,
                    exc_info=not isinstance(exc, UserError),
                )
                order._poa_log_failure(step, message)
                result["failed"] |= order
                result["errors"][order.id] = "%s: %s" % (labels.get(step, step), message)
                continue
            if report:
                result["processed"] |= order
            else:
                result["skipped"] |= order
        _logger.info(
            "Purchase automation batch: %s processed, %s skipped, %s failed",
            len(result["processed"]), len(result["skipped"]), len(result["failed"]),
        )
        return result

    @api.model
    def _poa_exception_message(self, exc):
        if isinstance(exc, UserError):
            return exc.args[0] if exc.args else str(exc)
        return "%s: %s" % (type(exc).__name__, exc)

    def _poa_process_single(self, opts, step_tracker=None):
        """Run the pipeline on one order. Returns a list of human-readable
        report lines (empty when there was nothing to do)."""
        self.ensure_one()
        tracker = step_tracker if step_tracker is not None else {}
        report = []

        if self.state == "cancel":
            return report
        self._poa_lock()

        tracker["step"] = STEP_CONFIRM
        report += self._poa_step_confirm(opts)

        tracker["step"] = STEP_RECEIPT
        report += self._poa_step_receipt(opts)

        tracker["step"] = STEP_BILL
        bill_date = self._poa_bill_date(opts)
        report += self._poa_step_bill(opts, bill_date)

        rank = SCENARIO_RANK.get(opts["scenario"], 1)
        if rank >= 2:
            tracker["step"] = STEP_POST
            report += self._poa_step_post(bill_date)
        if rank >= 3:
            tracker["step"] = STEP_PAYMENT
            report += self._poa_step_payment(opts, bill_date)

        if report:
            self._poa_log_success(opts, report)
        self.write({"poa_state": "done", "poa_last_error": False,
                    "poa_last_run": fields.Datetime.now()})
        return report

    def _poa_lock(self):
        """Row lock so two users cannot automate the same order concurrently."""
        try:
            with self.env.cr.savepoint(flush=False):
                self.env.cr.execute(
                    "SELECT id FROM purchase_order WHERE id = %s FOR UPDATE NOWAIT",
                    [self.id], log_exceptions=False,
                )
        except psycopg2.errors.LockNotAvailable:
            raise UserError(_("%s is being processed by another user. Try again in a moment.", self.name))

    # ------------------------------------------------------------------
    # Step 1 - confirmation
    # ------------------------------------------------------------------
    def _poa_step_confirm(self, opts):
        report = []
        warehouse = opts.get("warehouse")
        if self.state in ("draft", "sent"):
            if warehouse:
                if warehouse.company_id != self.company_id:
                    raise UserError(_(
                        "Warehouse %(wh)s does not belong to company %(company)s.",
                        wh=warehouse.name, company=self.company_id.name))
                if not warehouse.in_type_id:
                    raise UserError(_("Warehouse %s has no receipt operation type.", warehouse.name))
                self.picking_type_id = warehouse.in_type_id
            self.with_context(poa_skip_auto=True).button_confirm()
            report.append(_("Order confirmed."))
        elif warehouse and self.picking_type_id.warehouse_id != warehouse:
            report.append(_("Warehouse override ignored: the order was already confirmed."))

        if self.state == "to approve":
            if not self._approval_allowed():
                raise UserError(_(
                    "%s requires approval by a Purchase Administrator before it can be processed.",
                    self.name))
            self.with_context(poa_skip_auto=True).button_approve()
            report.append(_("Order approved."))

        if self.state != "purchase":
            raise UserError(_("%(order)s could not be confirmed (state: %(state)s).",
                              order=self.name, state=self.state))
        return report

    # ------------------------------------------------------------------
    # Step 2 - receipts
    # ------------------------------------------------------------------
    def _poa_incoming_pickings(self):
        return self.picking_ids.filtered(
            lambda p: p.picking_type_code == "incoming" and p.state not in ("done", "cancel"))

    def _poa_step_receipt(self, opts):
        report = []
        report += self._poa_receive_manual_lines()

        pickings = self._poa_incoming_pickings()
        if not pickings:
            pending = self.order_line.filtered(
                lambda l: not l.display_type
                and l.product_id.type == "consu"
                and l.qty_received_method == "stock_moves"
                and float_compare(l.qty_received, l.product_qty,
                                  precision_rounding=l.product_uom_id.rounding) < 0)
            if pending:
                raise UserError(_(
                    "No open receipt was found for %(order)s although these products are "
                    "not fully received: %(products)s.",
                    order=self.name,
                    products=", ".join(pending.product_id.mapped("display_name"))))
            return report

        never_backorder = opts.get("backorder_policy", "never") == "never"
        for picking in pickings:
            self._poa_fill_picking(picking, opts)
            # v17: the immediate-transfer wizard (and its skip_immediate key) no longer exists;
            # button_validate() only proposes the backorder wizard, which skip_backorder bypasses.
            ctx = {
                "skip_backorder": True,
                "skip_sms": True,
                "skip_expired": True,
            }
            if never_backorder:
                ctx["picking_ids_not_to_backorder"] = picking.ids
            res = picking.with_context(**ctx).button_validate()
            if picking.state != "done":
                wizard = isinstance(res, dict) and (res.get("res_model") or res.get("name")) or ""
                raise UserError(_(
                    "Receipt %(picking)s could not be validated automatically%(detail)s. "
                    "It probably requires a manual step (quality check, lot entry, ...).",
                    picking=picking.name,
                    detail=wizard and " (%s)" % wizard or ""))
            report.append(_("Receipt %s validated.", picking.name))

        backorders = self._poa_incoming_pickings() - pickings
        if backorders:
            report.append(_("Backorder(s) kept open: %s.", ", ".join(backorders.mapped("name"))))
        return report

    def _poa_receive_manual_lines(self):
        """Lines whose received quantity is manual (services) are marked fully received
        so that 'based on received quantities' billing control does not block the bill."""
        lines = self.order_line.filtered(
            lambda l: not l.display_type
            and l.qty_received_method == "manual"
            and float_compare(l.qty_received, l.product_qty,
                              precision_rounding=l.product_uom_id.rounding or 0.01) < 0)
        for line in lines:
            line.qty_received = line.product_qty
        return [_("%s service line(s) marked as received.", len(lines))] if lines else []

    def _poa_fill_picking(self, picking, opts):
        if picking.state == "draft":
            picking.action_confirm()
        if picking.state in ("confirmed", "waiting"):
            picking.action_assign()
        moves = picking.move_ids.filtered(lambda m: m.state not in ("done", "cancel"))
        for move in moves:
            # v17: `quantity` replaces `quantity_done` / reserved quantity; its inverse
            # creates or updates the move lines (one line per unit for serials).
            rounding = move.product_uom.rounding
            if float_compare(move.quantity, move.product_uom_qty, precision_rounding=rounding) < 0:
                move.quantity = move.product_uom_qty
            if move.has_tracking != "none":
                self._poa_assign_lots(move, opts)
        # v17: only moves flagged `picked` are processed by _action_done().
        moves.picked = True

    def _poa_assign_lots(self, move, opts):
        lines = move.move_line_ids.filtered(lambda ml: ml.quantity and not ml.lot_id and not ml.lot_name)
        if not lines:
            return
        product = move.product_id
        if not opts.get("auto_lots"):
            raise UserError(_(
                "%(product)s is tracked by lots/serial numbers. Enter them on %(picking)s, or run "
                "the batch wizard with 'Generate Lot/Serial Numbers' enabled.",
                product=product.display_name, picking=move.picking_id.name))
        if not move.picking_type_id.use_create_lots:
            raise UserError(_(
                "Operation type %s does not allow creating new lots/serial numbers.",
                move.picking_type_id.display_name))
        if move.has_tracking == "serial":
            odd = lines.filtered(lambda ml: float_compare(
                ml.quantity, 1.0, precision_rounding=ml.product_uom_id.rounding) != 0)
            if odd:
                raise UserError(_("Serial-tracked lines of %s must have a done quantity of 1.",
                                  product.display_name))
            names = self._poa_free_lot_names(product, len(lines))
            for line, name in zip(lines, names):
                line.lot_name = name
        else:
            lines.write({"lot_name": self._poa_free_lot_names(product, 1)[0]})

    def _poa_free_lot_names(self, product, count):
        code = product.default_code or str(product.id)
        base = re.sub(r"[^A-Za-z0-9]+", "-", "%s-%s" % (self.name, code)).strip("-")
        taken = set(self.env["stock.lot"].search([
            ("product_id", "=", product.id),
            ("company_id", "=", self.company_id.id),
            ("name", "=like", base + "%"),
        ]).mapped("name"))
        taken |= set(self.env["stock.move.line"].search([
            ("product_id", "=", product.id),
            ("lot_name", "=like", base + "%"),
            ("state", "not in", ("done", "cancel")),
        ]).mapped("lot_name"))
        names, seq = [], 1
        while len(names) < count:
            candidate = "%s-%04d" % (base, seq)
            if candidate not in taken:
                names.append(candidate)
            seq += 1
        return names

    # ------------------------------------------------------------------
    # Step 3 - bill creation
    # ------------------------------------------------------------------
    def _poa_bill_date(self, opts):
        if opts.get("bill_date"):
            return opts["bill_date"]
        source = opts.get("bill_date_source") or "today"
        if source == "receipt":
            done = self.picking_ids.filtered(
                lambda p: p.state == "done" and p.picking_type_code == "incoming" and p.date_done)
            if done:
                return fields.Date.context_today(self, max(done.mapped("date_done")))
        elif source == "order" and self.date_order:
            return fields.Date.context_today(self, self.date_order)
        return fields.Date.context_today(self)

    def _poa_bills(self):
        return self.invoice_ids.filtered(
            lambda m: m.poa_generated and m.move_type in ("in_invoice", "in_refund"))

    def _poa_step_bill(self, opts, bill_date):
        self.ensure_one()
        if self.invoice_status != "to invoice":
            return []
        journal = opts.get("bill_journal")
        if journal and journal.company_id != self.company_id:
            raise UserError(_("Bill journal %(journal)s does not belong to %(company)s.",
                              journal=journal.display_name, company=self.company_id.name))
        if journal and journal.type != "purchase":
            raise UserError(_("Journal %s is not a purchase journal.", journal.display_name))
        before = self.invoice_ids
        self.with_context(
            poa_generated=True,
            poa_bill_journal_id=journal.id if journal else False,
            poa_invoice_date=bill_date,
        ).action_create_invoice()
        new_bills = self.invoice_ids - before
        if not new_bills:
            raise UserError(_("No vendor bill was created for %s.", self.name))
        amounts = ", ".join(
            format_amount(self.env, bill.amount_total, bill.currency_id) for bill in new_bills)
        return [_("Vendor bill created in %(journal)s (%(amount)s).",
                  journal=", ".join(new_bills.journal_id.mapped("name")), amount=amounts)]

    # ------------------------------------------------------------------
    # Step 4 - posting
    # ------------------------------------------------------------------
    def _poa_step_post(self, bill_date):
        drafts = self._poa_bills().filtered(lambda m: m.state == "draft")
        if not drafts:
            return []
        for bill in drafts.filtered(lambda m: not m.invoice_date):
            bill.invoice_date = bill_date
        drafts.action_post()
        not_posted = drafts.filtered(lambda m: m.state != "posted")
        if not_posted:
            raise UserError(_("Vendor bill(s) %s could not be posted.",
                              ", ".join(not_posted.mapped("display_name"))))
        return [_("Vendor bill posted (%s).", ", ".join(drafts.mapped("name")))]

    # ------------------------------------------------------------------
    # Step 5 - payment
    # ------------------------------------------------------------------
    def _poa_step_payment(self, opts, bill_date):
        bills = self._poa_bills().filtered(
            lambda m: m.state == "posted" and m.move_type == "in_invoice"
            and m.payment_state in ("not_paid", "partial"))
        if not bills:
            return []
        journal = opts.get("payment_journal")
        if not journal:
            raise UserError(_(
                "Scenario C needs a payment journal. Set one in Purchase settings or in the wizard."))
        if journal.company_id != self.company_id:
            raise UserError(_("Payment journal %(journal)s does not belong to %(company)s.",
                              journal=journal.display_name, company=self.company_id.name))
        method = opts.get("payment_method_line")
        if method and method.journal_id != journal:
            method = self.env["account.payment.method.line"]
        method = method or journal.outbound_payment_method_line_ids[:1]
        if not method:
            raise UserError(_("Journal %s has no outgoing payment method.", journal.display_name))

        wizard = self.env["account.payment.register"].with_context(
            active_model="account.move",
            active_ids=bills.ids,
            dont_redirect_to_payments=True,
        ).create({
            "journal_id": journal.id,
            "payment_method_line_id": method.id,
            "payment_date": opts.get("payment_date") or bill_date,
            "group_payment": True,
        })
        wizard.action_create_payments()
        unpaid = bills.filtered(lambda m: m.payment_state not in ("paid", "in_payment"))
        if unpaid:
            raise UserError(_("Bill(s) %s are not fully reconciled after payment.",
                              ", ".join(unpaid.mapped("name"))))
        payments = bills._get_reconciled_payments()
        return [_("Payment registered (%s).", ", ".join(payments.mapped("name")))]

    # ------------------------------------------------------------------
    # Chatter / feedback
    # ------------------------------------------------------------------
    def _poa_log_success(self, opts, report):
        scenario = dict(POA_SCENARIOS).get(opts.get("scenario"), "")
        items = Markup("").join(Markup("<li>%s</li>") % line for line in report)
        body = Markup("<p><strong>%s</strong> <em>%s</em></p><ul>%s</ul>") % (
            _("Purchase automation completed."), scenario, items)
        self._message_log(body=body)

    def _poa_log_failure(self, step, message):
        label = _step_labels(self.env).get(step, step)
        body = Markup(
            "<div class='alert alert-warning mb-0' role='alert'>"
            "<p><strong>%s</strong></p><p>%s: %s</p>"
            "<p class='mb-0 small'>%s</p></div>"
        ) % (
            _("Purchase automation failed."), label, escape(message),
            _("All changes of this run were rolled back for this order. Fix the issue and "
              "run the automation again from the list view action."),
        )
        self._message_log(body=body)
        self.write({
            "poa_state": "failed",
            "poa_last_error": "%s: %s" % (label, message),
            "poa_last_run": fields.Datetime.now(),
        })

    def _poa_notification_action(self, result):
        processed, skipped, failed = (len(result[k]) for k in ("processed", "skipped", "failed"))
        message = _("%(ok)s processed, %(skip)s skipped, %(ko)s failed.",
                    ok=processed, skip=skipped, ko=failed)
        if failed:
            message += " " + _("Failed: %s (see chatter).", ", ".join(result["failed"].mapped("name")))
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Purchase Automation"),
                "message": message,
                "type": "warning" if failed else "success",
                "sticky": bool(failed),
                "next": {"type": "ir.actions.act_window_close"},
            },
        }

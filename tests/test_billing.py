"""Billing regressions using original layouts/names and controlled model outputs."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pymupdf

from grader.extract import pages_of
from grader.validate import citation_errors, schema_errors
from harness import billing as b
from harness import harness as h
from harness import workflow as w


def blank_record():
    return {"shipments": [], "payments": [], "credits": [], "waivers": [],
            "extensions": [], "mail_intent": "other", "mentioned_invoice_ids": [], "evidence": []}


def payment(invoice_id="BJ-100", **updates):
    value = {"invoice_id": invoice_id, "invoice_date": "2034-01-05", "due_date": "2034-01-30",
             "currency": "USD", "principal": "100.00", "open_amount": "100.00", "status": "unpaid",
             "paid_date": None, "scheduled_date": None, "disputed_amount": None,
             "principal_payments": [], "fees_paid": [], "evidence": []}
    value.update(updates)
    return value


def waiver(**updates):
    value = {"contract_id": "BLUEJAY-C71", "invoice_ids": [], "awbs": ["BJ-A1"],
             "month": None, "codes": ["X-DEL"], "full": True, "percentage": None,
             "amount": None, "currency": "USD", "valid_from": None, "valid_to": None,
             "conditions": [], "original_sender": None, "original_document_name": None, "evidence": []}
    value.update(updates)
    return value


class FormulaTests(unittest.TestCase):
    def test_storage_free_days_and_release_exclusion(self):
        self.assertEqual(b.storage_days("2034-01-02", "2034-01-09", 2), 5)
        self.assertEqual(b.storage_days("2034-01-02", "2034-01-03", 2), 0)
        with self.assertRaises(ValueError):
            b.storage_days("2034-01-09", "2034-01-02", 2)

    def test_currency_conversion_aggregates_before_rounding(self):
        invoice = {"currency": "INR", "invoice_date": "2034-01-05"}
        policy = {"fx_rows": [{"month": "2034-01", "inr_per_usd": "3", "evidence": []}]}
        total, _ = b.convert_finding(Decimal("0.37") + Decimal("0.37"), invoice, policy)
        self.assertEqual(total, Decimal("0.25"))

    def test_fx_uses_invoice_month_not_pickup_or_report_month(self):
        invoice = {"currency": "INR", "invoice_date": "2034-02-01"}
        policy = {"fx_rows": [{"month": "2034-01", "inr_per_usd": "80", "evidence": []},
                              {"month": "2034-02", "inr_per_usd": "100", "evidence": []}]}
        self.assertEqual(b.convert_finding(Decimal(1000), invoice, policy)[0], Decimal("10.00"))

    def test_missing_or_zero_fx_is_not_assumed(self):
        invoice = {"currency": "INR", "invoice_date": "2034-02-01"}
        with self.assertRaises(LookupError):
            b.convert_finding(Decimal(1), invoice, {"fx_rows": []})
        with self.assertRaises(ValueError):
            b.convert_finding(Decimal(1), invoice, {"fx_rows": [{"month": "2034-02", "inr_per_usd": "0", "evidence": []}]})

    def test_money_uses_half_up(self):
        self.assertEqual(b.money(Decimal("1.005")), Decimal("1.01"))

    def test_grace_is_inclusive_but_month_count_is_from_due_date(self):
        self.assertEqual(b.late_fee_schedule(Decimal(100), Decimal(2), "2034-01-01", "2034-01-06", 5, False), [])
        self.assertEqual(b.late_fee_schedule(Decimal(100), Decimal(2), "2034-01-01", "2034-01-07", 5, False), [Decimal("2.00")])
        self.assertEqual(len(b.late_fee_schedule(Decimal(100), Decimal(2), "2034-01-01", "2034-02-01", 5, False)), 2)

    def test_compounding_rounds_each_month(self):
        self.assertEqual(b.late_fee_schedule(Decimal(100), Decimal(2), "2034-01-01", "2034-03-03", 0, True),
                         [Decimal("2.00"), Decimal("2.04"), Decimal("2.08")])

    def test_simple_interest_does_not_compound(self):
        self.assertEqual(b.late_fee_schedule(Decimal(100), Decimal(2), "2034-01-01", "2034-03-03", 0, False),
                         [Decimal("2.00")] * 3)

    def test_paid_fees_leave_compounding_base(self):
        fees = [{"date": "2034-01-20", "amount": "2.00"}]
        self.assertEqual(b.late_fee_schedule(Decimal(100), Decimal(2), "2034-01-01", "2034-02-01", 0, True, fees_paid=fees),
                         [Decimal("2.00"), Decimal("2.00")])

    def test_principal_payments_reduce_later_months(self):
        payments = [{"date": "2034-01-15", "amount": "50.00"}]
        self.assertEqual(b.late_fee_schedule(Decimal(100), Decimal(2), "2034-01-01", "2034-02-01", 0, False,
                                           principal_payments=payments), [Decimal("2.00"), Decimal("1.00")])


class BillingPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.mailbox = self.root / "mailbox"
        self.batch = self.mailbox / "batch_01"
        self.batch.mkdir(parents=True)
        self.state = self.root / "state"
        self.responses = {}
        self.calls = []
        self.next_id = 1
        self.definitions = [("X-SEC", "security", "Safety screening"), ("X-DEL", "delivery", "Last mile"),
                            ("X-STO", "storage", "Bonded warehouse"), ("X-DG", "dangerous_goods", "Hazard acceptance"),
                            ("X-ICE", "dry_ice", "Refrigerant handling"), ("X-CUS", "customs", "Border examination"),
                            ("X-ACT", "account", "Account service"), ("X-PCT", "peak", "Peak add-on"),
                            ("X-COST", "cost", "Underlying carrier cost"), ("X-MAR", "margin", "Handling uplift"),
                            ("X-LATE", "late_fee", "Overdue financing"), ("X-TAX", "tax", "Tax")]
        self.contract_doc = self.add("Executed agreement BLUEJAY-C71, signed by both parties.\n"
            "Effective 2034-01-01; ends 2034-12-31; no automatic renewal.\n"
            "Treasury Schedule Z: January 2034 INR per USD 100.\n"
            "Carrier Bluejay Air. Only Schedule 1 waivers/payment_terms contacts can change those terms.\n"
            "Fiona Reed <fiona@bluejay.example>: waivers, payment_terms, notices, treasury, billing_disputes.\n"
            "Each kg security 2; shipment delivery 20; storage day 10, 2 free days; DG 100; dry ice 25; customs 30.\n"
            "Delivery requires carrier delivery; dry ice only cannot attract DG; carrier paperwork errors exempt customs.\n"
            "Payment 25 days; late fee 2% monthly simple, 5 grace days; disputed amounts excluded; extensions permitted.\n"
            + getattr(self, "cashback_clause", "")
            + "\n".join(f"{code}: {label}" for code, _, label in self.definitions), "contract", attachment="agreement.pdf")
        refs = self.refs(self.contract_doc)
        contacts = [{"category": category, "party": "carrier", "person": "Fiona Reed",
                     "email": "fiona@bluejay.example", "evidence": refs}
                    for category in ("waivers", "payment_terms", "notices", "treasury", "billing_disputes")]
        self.terms = {"contract_id": "BLUEJAY-C71", "binding_state": "executed", "changes_term": False,
                      "effective_date": "2034-01-01", "expiry_date": "2034-12-31", "auto_renews": False,
                      "renewal_months": None, "notice_days": None, "notice_deadline": None,
                      "extension_policy": "jointly_signed", "notice_sent_date": None,
                      "notice_for_expiry_date": None, "contacts": contacts, "evidence": refs}
        self.responses["contract_terms", self.contract_doc] = self.terms
        self.policy = {"contract_id": "BLUEJAY-C71", "carrier_name": "Bluejay Air",
                       "definitions": [{"code": code, "category": category, "labels": [label], "evidence": refs}
                                       for code, category, label in self.definitions],
                       "rules": [], "fx_annex_name": "Treasury Schedule Z",
                       "fx_rows": [{"month": "2034-01", "inr_per_usd": "100", "evidence": refs}],
                       "payment": {"due_days": 25, "monthly_rate": "2", "grace_days": 5, "compounds": False,
                                   "exclude_disputed": True, "extensions_permitted": True, "evidence": refs}, "evidence": refs}
        for code, unit, rate, condition in (("X-SEC", "kg", "2", "always"), ("X-DEL", "shipment", "20", "carrier_delivery"),
                ("X-STO", "storage_day", "10", "always"), ("X-DG", "shipment", "100", "dangerous_goods"),
                ("X-ICE", "shipment", "25", "dry_ice_only"), ("X-CUS", "shipment", "30", "shipper_customs_fault"),
                ("X-ACT", "month", "10", "always")):
            self.policy["rules"].append({"code": code, "lane": None, "currency": "USD", "valid_from": None,
                "valid_to": None, "continues": None, "unit": unit, "rate": rate, "minimum": None,
                "maximum": None, "base_codes": [], "free_days": 2 if unit == "storage_day" else None,
                "condition": condition, "evidence": refs})
        self.responses["billing_policy", self.contract_doc] = self.policy
        self.responses["cashback_terms", self.contract_doc] = {
            "contract_id": "BLUEJAY-C71", "spend_categories": [], "spend_codes": [], "spend_basis": "unknown",
            "net_charge_credits": None, "excluded_credit_types": [], "rebate": None, "margin": None,
            "annual": [], "evidence": []}

    def tearDown(self):
        self.temporary.cleanup()

    def refs(self, doc):
        document = self.responses["source", doc]
        return [{"doc": doc, "page": 1, "quote": document["pages"][0]}]

    def add(self, body, kind="other", *, received="2034-01-10", sender="operations@bluejay.example", attachment=None, status="original"):
        name = f"{self.next_id:04d}.eml"
        self.next_id += 1
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = "accounts@freshshipper.example"
        msg["Subject"] = "Account documents"
        msg["Date"] = received + "T12:00:00+00:00"
        # Email Date is RFC 5322, not ISO.
        from datetime import datetime
        from email.utils import format_datetime
        msg.replace_header("Date", format_datetime(datetime.fromisoformat(received + "T12:00:00+00:00")))
        msg.set_content(body if attachment is None else "Attached is the requested original document.")
        if attachment:
            with pymupdf.open() as pdf:
                page = pdf.new_page()
                page.insert_text((25, 25), body, fontsize=8)
                data = pdf.tobytes()
            msg.add_attachment(data, maintype="application", subtype="pdf", filename=attachment)
        raw = msg.as_bytes()
        (self.batch / name).write_bytes(raw)
        email_doc = f"batch_01/{name}"
        self._source(email_doc, pages_of(raw, "eml"), received, "other", status)
        doc = email_doc
        if attachment:
            doc += "#" + attachment
            self._source(doc, pages_of(data, "pdf"), received, kind, status)
        else:
            self._source(doc, pages_of(raw, "eml"), received, kind, status)
        return doc

    def _source(self, doc, pages, received, kind, status):
        self.responses["source", doc] = {"doc": doc, "pages": pages, "received_at": received + "T12:00:00+00:00"}
        self.responses["document_identification", doc] = {
            "kind": kind, "content_status": status, "contract_ids": [], "invoice_ids": [], "awbs": [], "lanes": [],
            "changes": [], "effective_date": None,
            "evidence": [{"doc": doc, "page": 1, "quote": pages[0]}] if kind != "other" else [],
        }
        self.responses["billing_record", doc] = blank_record()

    def invoice(self, rows, *, invoice_id="BJ-100", invoice_date="2034-01-05", currency="USD", due="2034-01-30", received="2034-01-10"):
        lines, annotations = [], []
        text = f"Invoice {invoice_id}; BLUEJAY-C71; date {invoice_date}; due {due}; currency {currency}\n"
        for index, values in enumerate(rows, 1):
            values = dict(values)
            code = values.pop("code")
            _, category, label = next(entry for entry in self.definitions if entry[0] == code)
            awb = values.pop("awb", "BJ-A1")
            month = values.pop("service_month", None)
            reference_id = values.pop("reference_invoice_id", None)
            first = values.pop("first_fee_month", None)
            count = values.pop("fee_month_count", None)
            description = values.pop("description", label)
            line = {"description": description, "awb": awb, "pickup_date": "2034-01-02" if awb else None,
                    "lane": "NORTH-SOUTH" if awb else None, "quantity": "1", "quantity_unit": "shpt",
                    "rate": None, "rate_kind": "unknown", "billed_amount": "20", "source_page": 1, "source_quote": ""}
            line.update(values)
            text += f"{index} {description} {awb or '-'} {line['pickup_date'] or '-'} {line['lane'] or '-'} {line['quantity']} {line['quantity_unit']} {line['billed_amount']} {month or ''} {reference_id or ''}\n"
            annotations.append({"line_index": index, "code": code, "category": category, "shipment_id": awb,
                "service_month": month, "service_key": None, "reference_invoice_id": reference_id,
                "first_fee_month": first, "fee_month_count": count, "evidence": []})
            lines.append(line)
        doc = self.add(text, "invoice", attachment=invoice_id + ".pdf", received=received)
        for line, annotation in zip(lines, annotations):
            line["source_quote"] = self.responses["source", doc]["pages"][0]
            annotation["evidence"] = self.refs(doc)
        self.responses["invoice", doc] = {"invoice_id": invoice_id, "contract_id": "BLUEJAY-C71", "invoice_date": invoice_date,
                                           "due_date": due, "currency": currency, "line_items": lines}
        self.responses["billing_lines", doc] = {"lines": annotations}
        return doc

    def record(self, field, entries, *, sender="operations@bluejay.example", received="2034-01-10", status="original"):
        doc = self.add("Original account record: " + json.dumps(entries), sender=sender, received=received, status=status)
        facts = self.responses["billing_record", doc]
        for item in entries:
            item["evidence"] = self.refs(doc)
        facts[field] = entries
        return doc

    def operations(self, **updates):
        value = {"awb": "BJ-A1", "arrival_date": "2034-01-02", "release_date": "2034-01-09", "delivery": "carrier",
                 "goods": "non_dangerous", "customs_fault": "shipper", "cost": None, "currency": None, "evidence": []}
        value.update(updates)
        return self.record("shipments", [value])

    def api(self, **kwargs):
        def create(**request_kwargs):
            request = json.loads(request_kwargs["messages"][0]["content"])
            task = request.get("task", "invoice")
            if "task" not in request and "requested_lane" in request:
                task = "contract_pricing"
            elif "task" not in request and "requested_awb" in request:
                task = "tracking"
            doc = request["document"]["doc"] if "document" in request else request["doc"]
            self.calls.append((task, doc))
            value = self.responses.get((task, doc), {"dependencies": []}) if task == "pricing_dependencies" else self.responses[task, doc]
            if callable(value):
                value = value(request)
            if "requested_fields" in request:
                value = {field: value[field] for field in request["requested_fields"]}
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(value))])
        return SimpleNamespace(messages=SimpleNamespace(create=create))

    def run_report(self, as_of="2034-01-20", mode="supervised"):
        for batch in sorted(self.mailbox.glob("batch_*")):
            h.ingest(str(batch), str(self.state))
        with patch.object(h, "Anthropic", self.api):
            report = h.report(as_of, mode, str(self.state))
        self.assertEqual(schema_errors(report), [])
        self.assertEqual(citation_errors(report, self.mailbox), [])
        return report

    def amounts(self, report, class_name):
        return {item["invoice_id"]: item["amount_usd"] for item in report["findings"] if item["class"] == class_name and item["verdict"] == "finding"}

    def test_all_accessorials_use_entitlement_and_contractual_units(self):
        self.invoice([{"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "25"},
                      {"code": "X-STO", "quantity": "7", "quantity_unit": "day", "billed_amount": "70"},
                      {"code": "X-DG", "quantity": "3", "billed_amount": "100"},
                      {"code": "X-ICE", "quantity": "2", "billed_amount": "50"},
                      {"code": "X-DEL", "billed_amount": "20"}, {"code": "X-CUS", "billed_amount": "30"},
                      {"code": "X-TAX", "awb": None, "billed_amount": "999"}])
        self.operations(goods="dry_ice_only", delivery="consignee_collection", customs_fault="carrier")
        report = self.run_report()
        self.assertEqual(self.amounts(report, "overcharge"), {"BJ-100": "200.00"})

    def test_mixed_dg_excludes_dry_ice_fee(self):
        self.invoice([{"code": "X-DG", "billed_amount": "100"}, {"code": "X-ICE", "billed_amount": "25"}])
        self.operations(goods="dangerous")
        self.assertEqual(self.amounts(self.run_report(), "overcharge"), {"BJ-100": "25.00"})

    def test_per_shipment_fee_not_per_package_or_duplicate_row(self):
        self.invoice([{"code": "X-ICE", "quantity": "5", "quantity_unit": "pkg", "billed_amount": "125"},
                      {"code": "X-ICE", "billed_amount": "25"}])
        self.operations(goods="dry_ice_only")
        self.assertEqual(self.amounts(self.run_report(), "overcharge"), {"BJ-100": "125.00"})

    def test_unknown_goods_cannot_be_assumed_non_dangerous(self):
        self.invoice([{"code": "X-DG", "billed_amount": "100"}])
        self.operations(goods="unknown")
        report = self.run_report()
        self.assertEqual(report["findings"], [])
        self.assertTrue(any("goods" in item["reason"] for item in report["diagnostics"]))

    def test_outside_company_declaration_cannot_change_goods_eligibility(self):
        self.invoice([{"code": "X-DG", "billed_amount": "100"}])
        self.operations(goods="dangerous")
        self.record("shipments", [{"awb": "BJ-A1", "arrival_date": None, "release_date": None,
                    "delivery": "unknown", "goods": "dry_ice_only", "customs_fault": "unknown",
                    "cost": None, "currency": None, "evidence": []}], sender="operations@outside.example")
        report = self.run_report()
        self.assertEqual(report["findings"], [])

    def test_pass_through_applies_explicit_minimum_and_cap(self):
        rule = {**copy.deepcopy(self.policy["rules"][0]), "code": "X-COST", "unit": "pass_through",
                "rate": None, "minimum": "30", "maximum": "50"}
        row = {"line": {}, "annotation": {"shipment_id": "BJ-A1"}}
        invoice = {"invoice": {"currency": "USD"}}
        for cost, expected in (("20", "30.00"), ("40", "40.00"), ("80", "50.00")):
            facts = blank_record()
            facts["shipments"] = [{"awb": "BJ-A1", "cost": cost, "currency": "USD", "evidence": self.refs(self.contract_doc)}]
            context = {"records": [{"facts": facts, "status": "original"}]}
            amount, _ = b._price_accessorial(rule, row, invoice, context)
            self.assertEqual(amount, Decimal(expected))

    def test_percentage_uses_rounded_correct_base(self):
        self.policy["rules"].append({**copy.deepcopy(self.policy["rules"][0]), "code": "X-PCT", "unit": "percentage",
                                     "rate": "10", "base_codes": ["X-SEC"]})
        self.invoice([{"code": "X-PCT", "billed_amount": "3"},
                      {"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "30"}])
        self.assertEqual(self.amounts(self.run_report(), "overcharge"), {"BJ-100": "11.00"})

    def test_inr_conversion_is_once_for_the_entire_finding(self):
        for rule in self.policy["rules"]:
            rule["currency"] = "INR"
        self.policy["fx_rows"][0]["inr_per_usd"] = "3"
        self.policy["rules"][0]["rate"] = "0.01"
        self.invoice([{"code": "X-SEC", "awb": "BJ-A1", "quantity": "1", "quantity_unit": "kg", "billed_amount": "0.38"},
                      {"code": "X-SEC", "awb": "BJ-A2", "quantity": "1", "quantity_unit": "kg", "billed_amount": "0.38"}], currency="INR")
        self.assertEqual(self.amounts(self.run_report(), "overcharge"), {"BJ-100": "0.25"})

    def test_missing_named_fx_requests_the_schedule_treasury_holder(self):
        for rule in self.policy["rules"]:
            rule["currency"] = "INR"
        self.policy["fx_rows"] = []
        self.invoice([{"code": "X-SEC", "quantity": "1", "quantity_unit": "kg", "billed_amount": "102"}], currency="INR")
        item = self.run_report()["findings"][0]
        self.assertEqual(item["verdict"], "cannot_determine")
        self.assertEqual(item["missing_doc"]["name"], "Treasury Schedule Z")
        self.assertEqual(item["missing_doc"]["holder"]["person"], "Fiona Reed")

    def test_different_invoice_duplicate_matches_code_despite_changed_label(self):
        self.invoice([{"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "20.00"}])
        self.invoice([{"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "20",
                       "description": "Safety inspection adjustment"}], invoice_id="BJ-200", invoice_date="2034-01-15")
        self.assertEqual(self.amounts(self.run_report(), "duplicate"), {"BJ-200": "20.00"})

    def test_house_awbs_do_not_collapse_into_the_master(self):
        self.invoice([{"code": "X-SEC", "awb": "BJ-MASTER-H1", "quantity": "10", "quantity_unit": "kg", "billed_amount": "20"}])
        self.invoice([{"code": "X-SEC", "awb": "BJ-MASTER-H2", "quantity": "10", "quantity_unit": "kg", "billed_amount": "20"}],
                     invoice_id="BJ-200", invoice_date="2034-01-15")
        self.assertEqual(self.amounts(self.run_report(), "duplicate"), {})

    def rebill(self, credit_amount):
        self.invoice([{"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "20"}])
        self.invoice([{"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "20"}], invoice_id="BJ-200", invoice_date="2034-01-15")
        credit = {"credit_id": "BJ-CN1", "credit_date": "2034-01-12", "contract_id": "BLUEJAY-C71", "invoice_ids": ["BJ-100"],
                  "awb": "BJ-A1", "code": "X-SEC", "service_month": None, "currency": "USD", "amount": credit_amount, "evidence": []}
        self.record("credits", [credit], received="2034-01-15")

    def test_fully_credited_rebill_is_not_a_duplicate(self):
        self.rebill("20")
        self.assertEqual(self.amounts(self.run_report(), "duplicate"), {})

    def test_partial_credit_reduces_duplicate_overlap(self):
        self.rebill("5")
        self.assertEqual(self.amounts(self.run_report(), "duplicate"), {"BJ-200": "15.00"})

    def test_credit_copies_are_counted_once(self):
        self.rebill("5")
        credit = next(facts["credits"][0] for (task, _), facts in self.responses.items() if task == "billing_record" and facts["credits"])
        self.record("credits", [copy.deepcopy(credit)], received="2034-01-16")
        self.assertEqual(self.amounts(self.run_report(), "duplicate"), {"BJ-200": "15.00"})

    def resend(self, intent="payment_request", status="paid"):
        self.invoice([{"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "20"}])
        doc = self.invoice([{"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "20"}], received="2034-01-18")
        cover = doc.partition("#")[0]
        self.responses["billing_record", cover].update(mail_intent=intent, mentioned_invoice_ids=["BJ-100"], evidence=self.refs(cover))
        self.record("payments", [payment(status=status, principal="20", open_amount="0" if status == "paid" else "20",
                                        paid_date="2034-01-12" if status == "paid" else None,
                                        scheduled_date="2034-01-25" if status == "scheduled" else None)])

    def test_paid_invoice_resend_with_payment_request_is_duplicate(self):
        self.resend()
        self.assertEqual(self.amounts(self.run_report(), "duplicate"), {"BJ-100": "20.00"})

    def test_scheduled_invoice_resend_is_duplicate_even_before_payment_day(self):
        self.resend(status="scheduled")
        self.assertEqual(self.amounts(self.run_report(), "duplicate"), {"BJ-100": "20.00"})

    def test_unpaid_reminder_is_not_duplicate(self):
        self.resend(intent="reminder", status="unpaid")
        self.assertEqual(self.amounts(self.run_report(), "duplicate"), {})

    def test_paid_copy_for_records_is_not_a_new_payment_request(self):
        self.resend(intent="copy", status="paid")
        self.assertEqual(self.amounts(self.run_report(), "duplicate"), {})

    def test_authorized_one_shipment_waiver_does_not_cover_another(self):
        self.invoice([{"code": "X-DEL", "billed_amount": "20"}])
        self.invoice([{"code": "X-DEL", "awb": "BJ-A2", "billed_amount": "20"}], invoice_id="BJ-200")
        self.operations()
        self.operations(awb="BJ-A2")
        self.record("waivers", [waiver()], sender="fiona@bluejay.example")
        self.assertEqual(self.amounts(self.run_report(), "waived_but_billed"), {"BJ-100": "20.00"})

    def test_unlisted_sender_has_no_waiver_authority(self):
        self.invoice([{"code": "X-DEL", "billed_amount": "20"}])
        self.operations()
        self.record("waivers", [waiver()], sender="outsider@bluejay.example")
        self.assertEqual(self.amounts(self.run_report(), "waived_but_billed"), {})

    def test_waiver_condition_needs_actual_payment_not_a_schedule(self):
        self.invoice([{"code": "X-DEL", "billed_amount": "20"}])
        self.operations()
        condition = {"kind": "payment_by", "invoice_id": "BJ-100", "awb": None, "deadline": "2034-01-15", "document_name": None}
        self.record("waivers", [waiver(conditions=[condition])], sender="fiona@bluejay.example")
        self.record("payments", [payment(status="scheduled", scheduled_date="2034-01-12")])
        self.assertEqual(self.amounts(self.run_report(), "waived_but_billed"), {})

    def test_met_payment_condition_activates_waiver(self):
        self.invoice([{"code": "X-DEL", "billed_amount": "20"}])
        self.operations()
        condition = {"kind": "payment_by", "invoice_id": "BJ-100", "awb": None, "deadline": "2034-01-15", "document_name": None}
        self.record("waivers", [waiver(conditions=[condition])], sender="fiona@bluejay.example")
        self.record("payments", [payment(status="paid", paid_date="2034-01-12", open_amount="0")])
        self.assertEqual(self.amounts(self.run_report(), "waived_but_billed"), {"BJ-100": "20.00"})

    def test_quoted_waiver_requests_original_without_binding(self):
        self.invoice([{"code": "X-DEL", "billed_amount": "20"}])
        self.operations()
        self.record("waivers", [waiver(original_sender="fiona@bluejay.example", original_document_name="Fiona's original waiver email")],
                    sender="forwarder@freshshipper.example", status="quoted_only")
        item = next(item for item in self.run_report()["findings"] if item["class"] == "waived_but_billed")
        self.assertEqual(item["verdict"], "cannot_determine")
        self.assertEqual(item["missing_doc"]["holder"]["person"], "Fiona Reed")

    def test_waiver_and_overcharge_do_not_double_count_the_waived_line(self):
        self.invoice([{"code": "X-DEL", "billed_amount": "25"}])
        self.operations()
        self.record("waivers", [waiver()], sender="fiona@bluejay.example")
        report = self.run_report()
        self.assertEqual(self.amounts(report, "waived_but_billed"), {"BJ-100": "25.00"})
        self.assertEqual(self.amounts(report, "overcharge"), {})

    def test_partial_waiver_keeps_remaining_rate_overcharge(self):
        self.invoice([{"code": "X-DEL", "billed_amount": "25"}])
        self.operations()
        self.record("waivers", [waiver(full=False, amount="10")], sender="fiona@bluejay.example")
        report = self.run_report()
        self.assertEqual(self.amounts(report, "waived_but_billed"), {"BJ-100": "10.00"})
        self.assertEqual(self.amounts(report, "overcharge"), {"BJ-100": "5.00"})

    def test_monthly_waiver_does_not_leak_into_the_next_month(self):
        self.invoice([{"code": "X-ACT", "awb": None, "service_month": "2034-01", "quantity_unit": "month", "billed_amount": "10"}])
        self.invoice([{"code": "X-ACT", "awb": None, "service_month": "2034-02", "quantity_unit": "month", "billed_amount": "10"}],
                     invoice_id="BJ-200", invoice_date="2034-02-05", received="2034-02-06")
        self.record("waivers", [waiver(awbs=[], month="2034-01", codes=["X-ACT"])], sender="fiona@bluejay.example")
        self.assertEqual(self.amounts(self.run_report("2034-02-20"), "waived_but_billed"), {"BJ-100": "10.00"})

    def test_authorized_due_extension_invalidates_billed_fee(self):
        self.invoice([{"code": "X-LATE", "awb": None, "reference_invoice_id": "BJ-PRINCIPAL", "first_fee_month": 1,
                       "fee_month_count": 1, "quantity_unit": "month", "billed_amount": "2"}])
        self.record("payments", [payment("BJ-PRINCIPAL", due_date="2034-01-01", status="paid", paid_date="2034-01-10", open_amount="0")])
        self.record("extensions", [{"invoice_id": "BJ-PRINCIPAL", "due_date": "2034-01-15", "evidence": []}], sender="fiona@bluejay.example")
        self.assertEqual(self.amounts(self.run_report(), "late_fee_not_owed"), {"BJ-100": "2.00"})

    def test_unlisted_due_extension_does_not_override_contract(self):
        self.invoice([{"code": "X-LATE", "awb": None, "reference_invoice_id": "BJ-PRINCIPAL", "first_fee_month": 1,
                       "fee_month_count": 1, "quantity_unit": "month", "billed_amount": "2"}])
        self.record("payments", [payment("BJ-PRINCIPAL", due_date="2034-01-01", status="paid", paid_date="2034-01-10", open_amount="0")])
        self.record("extensions", [{"invoice_id": "BJ-PRINCIPAL", "due_date": "2034-01-15", "evidence": []}], sender="outsider@bluejay.example")
        self.assertEqual(self.amounts(self.run_report(), "late_fee_not_owed"), {})

    def test_correct_late_fee_is_left_alone(self):
        self.invoice([{"code": "X-LATE", "awb": None, "reference_invoice_id": "BJ-PRINCIPAL", "first_fee_month": 1,
                       "fee_month_count": 1, "quantity_unit": "month", "billed_amount": "2"}])
        self.record("payments", [payment("BJ-PRINCIPAL", due_date="2034-01-01", status="paid", paid_date="2034-01-10", open_amount="0")])
        self.assertEqual(self.amounts(self.run_report(), "late_fee_not_owed"), {})

    def test_disputed_principal_is_removed_from_late_fee_base(self):
        self.invoice([{"code": "X-LATE", "awb": None, "reference_invoice_id": "BJ-PRINCIPAL", "first_fee_month": 1,
                       "fee_month_count": 1, "quantity_unit": "month", "billed_amount": "2"}])
        self.record("payments", [payment("BJ-PRINCIPAL", due_date="2034-01-01", status="paid", paid_date="2034-01-10", open_amount="0", disputed_amount="50")])
        self.assertEqual(self.amounts(self.run_report(), "late_fee_not_owed"), {"BJ-100": "1.00"})

    def test_risk_uses_open_principal_less_native_dispute_and_ignores_grace(self):
        self.invoice([{"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "30"}])
        self.record("payments", [payment(principal="30", open_amount="30")])
        self.policy["payment"]["grace_days"] = 999
        report = self.run_report()
        self.assertEqual(report["portfolio"]["late_fee_risk"][0]["amount_usd"], "0.40")
        self.assertEqual(report["portfolio"]["late_fee_risk"][0]["deadline"], "2034-01-30")

    def test_register_misclassified_as_invoice_does_not_hide_overcharge_or_payment_risk(self):
        self.invoice([{"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "25"},
                      {"code": "X-TAX", "awb": None, "billed_amount": "80"}])
        ledger = self.record("payments", [payment(principal="105", open_amount="105")])
        self.responses["document_identification", ledger].update(kind="invoice", evidence=self.refs(ledger))
        self.responses["invoice", ledger] = {"invoice_id": "BJ-100", "contract_id": None,
            "invoice_date": "2034-01-05", "due_date": "2034-01-30", "currency": "USD", "line_items": []}
        report = self.run_report()
        self.assertEqual(self.amounts(report, "overcharge"), {"BJ-100": "5.00"})
        self.assertEqual(report["portfolio"]["late_fee_risk"][0]["amount_usd"], "2.00")
        self.assertEqual(report["totals"]["total_savings"], "7.00")
        self.assertEqual(report["diagnostics"], [])
        index = json.loads((self.state / "document_index.json").read_text())
        self.assertEqual(next(doc for doc in index["documents"] if doc["doc"] == ledger)["kind"], "payment_ledger")
        with patch.object(h, "Anthropic", side_effect=AssertionError("Use existing valid caches")):
            self.assertEqual(h.report("2034-01-20", "supervised", str(self.state)), report)

    def test_different_itemized_invoice_copies_still_require_reconciliation(self):
        self.invoice([{"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "25"}])
        self.invoice([{"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "35"}])
        report = self.run_report()
        self.assertEqual(report["findings"], [])
        self.assertTrue(any(item["reason"] == "Different invoice copies require reconciliation."
                            for item in report["diagnostics"]))

    def test_unitemized_source_without_supported_records_is_reported_in_diagnostics(self):
        doc = self.add("Document BJ-EMPTY mentions a future invoice without itemized charges.", "invoice")
        self.responses["invoice", doc] = {"invoice_id": "BJ-EMPTY", "contract_id": None,
            "invoice_date": None, "due_date": None, "currency": None, "line_items": []}
        report = self.run_report()
        self.assertEqual(report["findings"], [])
        self.assertTrue(any(item.get("doc") == doc and "no itemized charges" in item["reason"]
                            for item in report["diagnostics"]))

    def test_inr_payment_risk_subtracts_inr_before_converting_the_fee(self):
        self.policy["rules"][0].update(currency="INR", rate="100")
        self.invoice([{"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "2000"}], currency="INR")
        self.record("payments", [payment(currency="INR", principal="2000", open_amount="2000")])
        report = self.run_report()
        self.assertEqual(self.amounts(report, "overcharge"), {"BJ-100": "10.00"})
        self.assertEqual(report["portfolio"]["late_fee_risk"][0]["amount_usd"], "0.20")

    def test_paid_invoice_has_no_payment_risk(self):
        self.invoice([{"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "20"}])
        self.record("payments", [payment(status="paid", paid_date="2034-01-18", principal="20", open_amount="0")])
        self.assertEqual(self.run_report()["portfolio"]["late_fee_risk"], [])

    def test_payment_risk_window_is_one_to_fourteen_days(self):
        self.invoice([{"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "20"}])
        self.record("payments", [payment(principal="20", open_amount="20")])
        self.assertEqual(self.run_report("2034-01-15")["portfolio"]["late_fee_risk"], [])
        self.assertEqual(len(self.run_report("2034-01-16")["portfolio"]["late_fee_risk"]), 1)
        self.assertEqual(self.run_report("2034-01-30")["portfolio"]["late_fee_risk"], [])

    def test_extension_moves_payment_risk_out_of_window(self):
        self.invoice([{"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "20"}])
        self.record("payments", [payment(principal="20", open_amount="20")])
        self.record("extensions", [{"invoice_id": "BJ-100", "due_date": "2034-02-28", "evidence": []}], sender="fiona@bluejay.example")
        self.assertEqual(self.run_report()["portfolio"]["late_fee_risk"], [])

    def test_future_waiver_and_payment_record_do_not_leak_into_earlier_report(self):
        self.invoice([{"code": "X-DEL", "billed_amount": "20"}])
        self.operations()
        self.record("waivers", [waiver()], sender="fiona@bluejay.example", received="2034-01-25")
        self.assertEqual(self.amounts(self.run_report("2034-01-20"), "waived_but_billed"), {})
        self.assertEqual(self.amounts(self.run_report("2034-01-26"), "waived_but_billed"), {"BJ-100": "20.00"})

    def test_fresh_process_can_replay_extended_audits_from_cache(self):
        self.invoice([{"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "25"}])
        first = self.run_report()
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, str(root / "harness/harness.py"), "report", "--as-of", "2034-01-20",
                                 "--state-dir", str(self.state)], cwd=self.root, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), first)

    def test_duplicate_charge_does_not_also_create_an_overcharge(self):
        self.invoice([{"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "30"}])
        self.invoice([{"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "30"},
                      {"code": "X-SEC", "awb": "BJ-A2", "quantity": "15", "quantity_unit": "kg", "billed_amount": "30"}],
                     invoice_id="BJ-200", invoice_date="2034-01-15")
        self.record("payments", [payment("BJ-200", principal="60", open_amount="60")])
        report = self.run_report()
        self.assertEqual(self.amounts(report, "overcharge"), {"BJ-100": "10.00"})
        self.assertEqual(self.amounts(report, "duplicate"), {"BJ-200": "30.00"})
        self.assertEqual(report["portfolio"]["late_fee_risk"][0]["amount_usd"], "0.60")

    def test_partial_duplicate_overlap_can_have_different_amounts(self):
        self.invoice([{"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "20"}])
        self.invoice([{"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "10"}],
                     invoice_id="BJ-200", invoice_date="2034-01-15")
        self.assertEqual(self.amounts(self.run_report(), "duplicate"), {"BJ-200": "10.00"})

    def test_credit_can_explicitly_reverse_shipment_code_without_invoice_id(self):
        self.rebill("5")
        credit = next(facts["credits"][0] for (task, _), facts in self.responses.items() if task == "billing_record" and facts["credits"])
        credit["invoice_ids"] = []
        self.assertEqual(self.amounts(self.run_report(), "duplicate"), {"BJ-200": "15.00"})

    def test_monthly_account_duplicate_uses_month_instead_of_an_awb(self):
        rows = [{"code": "X-ACT", "awb": None, "service_month": "2034-01", "quantity_unit": "month", "billed_amount": "10"}]
        self.invoice(rows)
        self.invoice(rows, invoice_id="BJ-200", invoice_date="2034-01-15")
        self.assertEqual(self.amounts(self.run_report(), "duplicate"), {"BJ-200": "10.00"})

    def test_different_service_months_are_not_duplicates(self):
        self.invoice([{"code": "X-ACT", "awb": None, "service_month": "2034-01", "quantity_unit": "month", "billed_amount": "10"}])
        self.invoice([{"code": "X-ACT", "awb": None, "service_month": "2034-02", "quantity_unit": "month", "billed_amount": "10"}],
                     invoice_id="BJ-200", invoice_date="2034-02-15", received="2034-02-16")
        self.assertEqual(self.amounts(self.run_report("2034-02-20"), "duplicate"), {})

    def test_amount_limited_waiver_is_capped_across_its_named_shipments(self):
        self.invoice([{"code": "X-DEL", "billed_amount": "20"}])
        self.invoice([{"code": "X-DEL", "awb": "BJ-A2", "billed_amount": "20"}], invoice_id="BJ-200", invoice_date="2034-01-06")
        self.operations()
        self.operations(awb="BJ-A2")
        self.record("waivers", [waiver(full=False, amount="10", awbs=["BJ-A1", "BJ-A2"])], sender="fiona@bluejay.example")
        self.assertEqual(self.amounts(self.run_report(), "waived_but_billed"), {"BJ-100": "10.00"})

    def test_condition_payment_proof_is_in_waiver_evidence(self):
        self.invoice([{"code": "X-DEL", "billed_amount": "20"}])
        self.operations()
        condition = {"kind": "payment_by", "invoice_id": "BJ-100", "awb": None, "deadline": "2034-01-15", "document_name": None}
        self.record("waivers", [waiver(conditions=[condition])], sender="fiona@bluejay.example")
        ledger_doc = self.record("payments", [payment(status="paid", paid_date="2034-01-12", open_amount="0")])
        item = next(item for item in self.run_report()["findings"] if item["class"] == "waived_but_billed")
        self.assertIn(ledger_doc, {ref["doc"] for ref in item["evidence"]})

    def test_held_original_waiver_removes_need_to_request_a_quoted_copy(self):
        self.invoice([{"code": "X-DEL", "billed_amount": "20"}])
        self.operations()
        self.record("waivers", [waiver(original_sender="fiona@bluejay.example", original_document_name="Fiona's original waiver email")],
                    status="quoted_only", sender="forwarder@freshshipper.example")
        self.record("waivers", [waiver()], sender="fiona@bluejay.example")
        report = self.run_report()
        self.assertEqual(self.amounts(report, "waived_but_billed"), {"BJ-100": "20.00"})
        self.assertFalse(any(item["verdict"] == "cannot_determine" for item in report["findings"]))

    def test_same_interest_month_billed_twice_is_not_owed_twice(self):
        row = {"code": "X-LATE", "awb": None, "reference_invoice_id": "BJ-PRINCIPAL", "first_fee_month": 1,
               "fee_month_count": 1, "quantity_unit": "month", "billed_amount": "2"}
        self.invoice([row, row])
        self.record("payments", [payment("BJ-PRINCIPAL", due_date="2034-01-01", status="paid", paid_date="2034-01-10", open_amount="0")])
        self.assertEqual(self.amounts(self.run_report(), "late_fee_not_owed"), {"BJ-100": "2.00"})

    def test_future_payment_date_does_not_remove_present_risk(self):
        self.invoice([{"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "20"}])
        self.record("payments", [payment(status="paid", paid_date="2034-01-28", principal="20", open_amount="0")])
        self.assertEqual(self.run_report()["portfolio"]["late_fee_risk"][0]["amount_usd"], "0.40")

    def test_pass_through_and_percentage_dependency_ignore_row_order(self):
        template = self.policy["rules"][0]
        self.policy["rules"].append({**copy.deepcopy(template), "code": "X-COST", "unit": "pass_through", "rate": None})
        self.policy["rules"].append({**copy.deepcopy(template), "code": "X-PCT", "unit": "percentage", "rate": "10", "base_codes": ["X-COST"]})
        self.invoice([{"code": "X-PCT", "billed_amount": "12"}, {"code": "X-COST", "billed_amount": "110"}])
        self.operations(cost="100.05", currency="USD")
        self.assertEqual(self.amounts(self.run_report(), "overcharge"), {"BJ-100": "11.94"})

    def test_aggregate_outbound_threshold_applies_across_finding_classes(self):
        self.invoice([{"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "3020"},
                      {"code": "X-DEL", "billed_amount": "2000"}])
        self.operations()
        self.record("waivers", [waiver()], sender="fiona@bluejay.example")
        report = self.run_report()
        self.assertEqual(report["totals"]["total_overpayment"], "5000.00")
        self.assertEqual({item["action"] for item in report["findings"]}, {"human_review"})

    def test_explicit_negative_invoice_credit_offsets_overcharge(self):
        self.invoice([{"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "30"},
                      {"code": "X-SEC", "billed_amount": "-5", "description": "Security credit adjustment"}])
        self.assertEqual(self.amounts(self.run_report(), "overcharge"), {"BJ-100": "5.00"})

    def test_negative_fee_credit_offsets_late_fee_dispute(self):
        self.invoice([{"code": "X-LATE", "awb": None, "reference_invoice_id": "BJ-PRINCIPAL", "first_fee_month": 1,
                       "fee_month_count": 1, "quantity_unit": "month", "billed_amount": "3"},
                      {"code": "X-LATE", "awb": None, "billed_amount": "-0.50"}])
        self.record("payments", [payment("BJ-PRINCIPAL", due_date="2034-01-01", status="paid", paid_date="2034-01-10", open_amount="0")])
        self.assertEqual(self.amounts(self.run_report(), "late_fee_not_owed"), {"BJ-100": "0.50"})

    def test_cross_batch_duplicate_arrives_after_the_first_report(self):
        self.invoice([{"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "20"}])
        doc = self.invoice([{"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "20"}],
                           invoice_id="BJ-200", invoice_date="2034-01-25", received="2034-01-26")
        cover = doc.partition("#")[0]
        second = self.mailbox / "batch_02"
        second.mkdir()
        (self.batch / cover.split("/")[1]).rename(second / cover.split("/")[1])
        for (task, source), value in list(self.responses.items()):
            if source == cover or source.startswith(cover + "#"):
                renamed = source.replace("batch_01/", "batch_02/", 1)
                encoded = json.dumps(value).replace(cover, cover.replace("batch_01/", "batch_02/", 1))
                self.responses[task, renamed] = json.loads(encoded)
                del self.responses[task, source]
        self.assertEqual(self.amounts(self.run_report("2034-01-20"), "duplicate"), {})
        self.assertEqual(self.amounts(self.run_report("2034-02-01"), "duplicate"), {"BJ-200": "20.00"})


if __name__ == "__main__":
    unittest.main()

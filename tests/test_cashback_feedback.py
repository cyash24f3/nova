"""Cashback and human learning regressions with controlled, source-checked API responses."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from harness import cashback as c, feedback as f, harness as h
import test_billing as fixtures

payment, waiver = fixtures.payment, fixtures.waiver


def envelope(items, decided="2034-02-01T12:00:00Z"):
    return {"schema_version": "1.0", "world": "OWN", "after_batch": "batch_01",
            "decided_at": decided, "decided_by": {"name": "AP Reviewer", "role": "AP Manager", "entity": "Fresh"},
            "items": items}


def decision(kind="close", fact_type="dispute_closed", invoice_id="BJ-100", **updates):
    item = {"item_id": "own-feedback-1", "solicited": False, "decision": kind,
            "finding_key": {"invoice_id": invoice_id, "class": "overcharge"},
            "fact": {"type": fact_type, "subject": {"invoice_id": invoice_id},
                     "value": {"status": "closed"}, "evidence": "AP recorded the carrier's decision."},
            "scope": {"kind": "one_off", "applies_to": {"invoice_id": invoice_id},
                      "valid_from": "2034-02-01", "valid_to": None}, "note": "Scoped regression decision"}
    item.update(updates)
    return item


class FormulaTests(unittest.TestCase):
    def test_all_spend_threshold_is_inclusive(self):
        rule = {"tier_basis": "all_spend", "tiers": [{"threshold_usd": "100", "percentage": "5"},
                                                      {"threshold_usd": "200", "percentage": "10"}]}
        self.assertEqual(c.rebate_amount(Decimal("99.99"), rule), Decimal(0))
        self.assertEqual(c.rebate_amount(Decimal("100"), rule), Decimal("5.00"))
        self.assertEqual(c.rebate_amount(Decimal("200"), rule), Decimal("20.00"))

    def test_incremental_tiers_do_not_reprice_the_whole_quarter(self):
        rule = {"tier_basis": "incremental", "tiers": [{"threshold_usd": "100", "percentage": "5"},
                                                       {"threshold_usd": "200", "percentage": "10"}]}
        self.assertEqual(c.rebate_amount(Decimal(250), rule), Decimal("10.00"))

    def test_ambiguous_tiers_fail_instead_of_guessed_money(self):
        with self.assertRaises(ValueError):
            c.rebate_amount(Decimal(300), {"tier_basis": "all_spend", "tiers":
                                         [{"threshold_usd": "100", "percentage": "5"},
                                          {"threshold_usd": "100", "percentage": "7"}]})

    def test_quarter_end_handles_leap_year_and_calendar_boundary(self):
        self.assertEqual(c.quarter("2032-02-29"), ("2032-Q1", date(2032, 1, 1), date(2032, 3, 31)))
        self.assertEqual(c.quarter("2034-12-31")[2], date(2034, 12, 31))


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.BillingPipelineTests(methodName="runTest")
        self.fixture.cashback_clause = (
            "QS is billed X-SEC security, X-ACT account, X-COST cost and X-MAR margin, net of charge credits dated in the period.\n"
            "Tax, duty, late fees, rebate and annual improvement credits are excluded. Duplicate invoice charges count until credited.\n"
            "Each calendar quarter, QS at least USD 100 earns 5% on all QS; USD 200 earns 10% on all QS. Credit due 30 days after quarter end.\n"
            "Annual improvement: 2% of prior contract-year QS, due 60 days after the effective-date anniversary.\n"
            "Quarter QS at least USD 100 steps X-MAR from 20% to 10% of correct X-COST after the crossing invoice, due 30 days after quarter end.\n")
        self.fixture.cashback_clause += ("Freight: NORTH-SOUTH and WEST-EAST USD 4/kg, all weights, minimum 0; pickup dates 2034-01-01 to 2034-12-31.\n"
                                        "X-COST is pass-through of documented actual cost; X-MAR is 20% of correct X-COST.\n"
                                        "Treasury Schedule Z: February 2034 INR per USD 200.\n")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        x = self.fixture
        original_invoice = x.invoice
        def invoice(rows, **kwargs):
            return original_invoice([{**row, "quantity_unit": row.get("quantity_unit", "month")} if row["code"] == "X-ACT" else row
                                     for row in rows], **kwargs)
        x.invoice = invoice
        refs = x.refs(x.contract_doc)
        self.cash = {"contract_id": "BLUEJAY-C71", "spend_categories": ["security", "account", "cost", "margin"],
                     "spend_codes": [], "spend_basis": "billed", "net_charge_credits": True,
                     "excluded_credit_types": ["rebate", "promised_credit"],
                     "rebate": {"tier_basis": "all_spend", "tiers":
                                [{"threshold_usd": "100", "percentage": "5", "evidence": refs},
                                 {"threshold_usd": "200", "percentage": "10", "evidence": refs}],
                                "due_days": 30, "evidence": refs},
                     "margin": None, "annual": [], "evidence": refs}
        x.responses["cashback_terms", x.contract_doc] = self.cash

    def cashback(self, report, period="2034-Q1", kind="rebate"):
        return next((item for item in report["portfolio"]["cashback"] if item["period"] == period and item["credit_type"] == kind
                     and item["verdict"] != "no_finding"), None)

    def credit(self, amount, issued="2034-04-02", *, kind="rebate", period="2034-Q1", identity="CN-1", lines=None, received=None):
        x = self.fixture
        doc = x.add(f"Issued credit {identity}; BLUEJAY-C71; {issued}; USD {amount}; {kind}; {period or ''}",
                    "credit_note", received=received or issued, attachment=identity + ".pdf")
        rows = copy.deepcopy(lines or [])
        for row in rows:
            row["evidence"] = x.refs(doc)
        x.responses["cashback_credits", doc] = {"credits": [{
            "credit_id": identity, "contract_id": "BLUEJAY-C71", "credit_date": issued,
            "invoice_ids": [],
            "credit_type": kind, "period": period, "currency": "USD", "amount": amount,
            "lines": rows, "evidence": x.refs(doc)}]}
        return doc

    def save_feedback(self, item, decided="2034-02-01T12:00:00Z"):
        x = self.fixture
        h.ingest(str(x.batch), str(x.state))
        file = x.root / "decisions.json"
        file.write_text(json.dumps(envelope([item], decided)))
        h.feedback(str(file), str(x.state))
        return file

    def test_closed_period_only_and_track_then_claim_on_due_date(self):
        x = self.fixture
        x.invoice([{"code": "X-ACT", "awb": None, "service_month": "2034-01", "billed_amount": "100"}])
        self.assertIsNone(self.cashback(x.run_report("2034-03-31")))
        item = self.cashback(x.run_report("2034-04-01"))
        self.assertEqual((item["amount_usd"], item["action"], item["deadline"]), ("5.00", "internal:track_credit", "2034-04-30"))
        self.assertEqual(self.cashback(x.run_report("2034-04-30"))["action"], "draft:claim_credit")

    def test_billed_not_corrected_or_paid_spend_and_tax_exclusion(self):
        x = self.fixture
        x.invoice([{"code": "X-ACT", "awb": None, "service_month": "2034-01", "billed_amount": "200"},
                   {"code": "X-TAX", "awb": None, "billed_amount": "1000"}])
        report = x.run_report("2034-04-30")
        self.assertEqual(self.cashback(report)["amount_usd"], "20.00")
        self.assertEqual(report["totals"]["cashback_owed"], "20.00")
        self.assertEqual(report["totals"]["total_overpayment"], "190.00")

    def test_established_earnings_with_unknown_deadline_are_tracked(self):
        x = self.fixture
        self.cash["rebate"]["due_days"] = None
        x.invoice([{"code": "X-ACT", "awb": None, "service_month": "2034-01", "billed_amount": "100"}])
        item = self.cashback(x.run_report("2034-04-30"))
        self.assertEqual((item["amount_usd"], item["action"], item["deadline"]), ("5.00", "internal:track_credit", None))

    def test_duplicate_distinct_invoice_ids_count_until_credit_reverses(self):
        x = self.fixture
        for identity in ("BJ-100", "BJ-200"):
            x.invoice([{"code": "X-ACT", "awb": None, "service_month": "2034-01", "billed_amount": "100"}], invoice_id=identity)
        self.assertEqual(self.cashback(x.run_report("2034-04-30"))["amount_usd"], "20.00")

    def test_same_invoice_copy_is_not_an_additional_spend(self):
        x = self.fixture
        for _ in range(2):
            x.invoice([{"code": "X-ACT", "awb": None, "service_month": "2034-01", "billed_amount": "100"}])
        self.assertEqual(self.cashback(x.run_report("2034-04-30"))["amount_usd"], "5.00")

    def test_issued_credit_is_subtracted_even_before_due_date(self):
        x = self.fixture
        x.invoice([{"code": "X-ACT", "awb": None, "service_month": "2034-01", "billed_amount": "100"}])
        self.credit("2")
        item = self.cashback(x.run_report("2034-04-02"))
        self.assertEqual((item["amount_usd"], item["action"]), ("3.00", "internal:track_credit"))
        self.credit("3", "2034-04-03", identity="CN-2")
        self.assertIsNone(self.cashback(x.run_report("2034-04-03")))

    def test_credit_type_and_period_are_not_interchangeable(self):
        x = self.fixture
        x.invoice([{"code": "X-ACT", "awb": None, "service_month": "2034-01", "billed_amount": "100"}])
        self.credit("5", kind="promised_credit", period="CY1")
        self.credit("5", identity="CN-2", period="2033-Q4")
        self.assertEqual(self.cashback(x.run_report("2034-04-30"))["amount_usd"], "5.00")

    def test_charge_credit_issue_date_reduces_qs_not_referenced_invoice_date(self):
        x = self.fixture
        x.invoice([{"code": "X-ACT", "awb": None, "service_month": "2034-01", "billed_amount": "200"}])
        self.credit("100", "2034-03-02", kind="charge_reversal", period=None,
                    lines=[{"code": "X-ACT", "category": "account", "amount": "100"}])
        item = self.cashback(x.run_report("2034-04-30"))
        self.assertEqual((item["amount_usd"], item["calculation"]["qualifying_spend_usd"]), ("5.00", "100.00"))

    def test_future_receipt_does_not_reduce_earlier_report(self):
        x = self.fixture
        x.invoice([{"code": "X-ACT", "awb": None, "service_month": "2034-01", "billed_amount": "100"}])
        self.credit("5", received="2034-05-01")
        self.assertEqual(self.cashback(x.run_report("2034-04-30"))["amount_usd"], "5.00")
        self.assertIsNone(self.cashback(x.run_report("2034-05-01")))

    def test_contract_year_and_anniversary_due_date(self):
        x = self.fixture
        self.cash["annual"] = [{"percentage": "2", "fixed_usd": None, "minimum_spend_usd": None, "due_days": 60,
                                "evidence": x.refs(x.contract_doc)}]
        x.invoice([{"code": "X-ACT", "awb": None, "service_month": "2034-01", "billed_amount": "100"}])
        self.assertIsNone(self.cashback(x.run_report("2034-12-31"), "CY1", "promised_credit"))
        item = self.cashback(x.run_report("2035-01-01"), "CY1", "promised_credit")
        self.assertEqual((item["amount_usd"], item["deadline"], item["action"]), ("2.00", "2035-03-02", "internal:track_credit"))

    def test_fixed_annual_credit_does_not_repeat_after_contract_ends(self):
        x = self.fixture
        self.cash["annual"] = [{"percentage": None, "fixed_usd": "25", "minimum_spend_usd": None, "due_days": 60,
                                "evidence": x.refs(x.contract_doc)}]
        report = x.run_report("2036-03-02")
        self.assertEqual([item["period"] for item in report["portfolio"]["cashback"]], ["CY1"])

    def test_cashback_threshold_requires_human_review(self):
        x = self.fixture
        x.invoice([{"code": "X-ACT", "awb": None, "service_month": "2034-01", "billed_amount": "50000"}])
        self.assertEqual(self.cashback(x.run_report("2034-04-30"))["action"], "human_review")

    def test_margin_crossing_invoice_keeps_old_rate_and_savings_do_not_overlap(self):
        x = self.fixture
        refs = x.refs(x.contract_doc)
        self.cash["margin"] = {"threshold_usd": "200", "rate_before": "20", "rate_after": "10",
                               "base_codes": ["X-COST"], "margin_codes": ["X-MAR"],
                               "trigger": "after_crossing_invoice", "due_days": 30, "evidence": refs}
        original = copy.deepcopy(x.policy["rules"][0])
        x.policy["rules"] += [{**original, "code": "X-COST", "unit": "pass_through", "rate": None},
                             {**original, "code": "X-MAR", "unit": "percentage", "rate": "20", "base_codes": ["X-COST"]}]
        for number, margin in ((1, "20"), (2, "20"), (3, "30")):
            awb = f"BJ-M{number}"
            x.invoice([{"code": "X-COST", "awb": awb, "billed_amount": "100"},
                       {"code": "X-MAR", "awb": awb, "billed_amount": margin, "quantity": "100", "rate_kind": "percentage"}],
                      invoice_id=f"BJ-MAR-{number}", invoice_date=f"2034-01-0{number + 4}")
            x.operations(awb=awb, cost="100", currency="USD")
        report = x.run_report("2034-04-30")
        self.assertEqual(self.cashback(report, kind="margin_step_down")["amount_usd"], "10.00")
        self.assertEqual(x.amounts(report, "overcharge"), {"BJ-MAR-3": "10.00"})
        self.assertEqual(self.cashback(report)["calculation"]["qualifying_spend_usd"], "370.00")

    def test_qualifying_spend_converts_each_invoice_at_its_own_month(self):
        x = self.fixture
        x.policy["fx_rows"].append({"month": "2034-02", "inr_per_usd": "200", "evidence": x.refs(x.contract_doc)})
        x.invoice([{"code": "X-ACT", "awb": None, "service_month": "2034-01", "billed_amount": "10000"}], currency="INR")
        x.invoice([{"code": "X-ACT", "awb": None, "service_month": "2034-02", "billed_amount": "10000"}],
                  invoice_id="BJ-200", currency="INR", invoice_date="2034-02-05", received="2034-02-05")
        item = self.cashback(x.run_report("2034-04-30"))
        self.assertEqual((item["amount_usd"], item["calculation"]["qualifying_spend_usd"]), ("7.50", "150.00"))

    def test_missing_fx_is_a_named_request_not_guessed_cashback(self):
        x = self.fixture
        x.invoice([{"code": "X-ACT", "awb": None, "service_month": "2034-02", "billed_amount": "10000"}],
                  currency="INR", invoice_date="2034-02-05", received="2034-02-05")
        item = self.cashback(x.run_report("2034-04-30"))
        self.assertEqual((item["verdict"], item["amount_usd"], item["missing_doc"]["name"]),
                         ("cannot_determine", None, "Treasury Schedule Z"))

    def pricing_fixture(self):
        x = self.fixture
        x.definitions.append(("X-FRT", "base_freight", "Air freight"))
        x.policy["definitions"].append({"code": "X-FRT", "category": "base_freight", "labels": ["Air freight"],
                                       "evidence": x.refs(x.contract_doc)})
        def prices(doc, rate, lane="NORTH-SOUTH", start="2034-01-01"):
            refs = [{"source_page": 1, "source_quote": x.responses["source", doc]["pages"][0]}]
            return {"contract_id": "BLUEJAY-C71", "effective_date": start, "expiry_date": "2034-12-31",
                    "pricing_date_basis": "pickup_date",
                    "freight_rates": [{"lane": lane, "currency": "USD", "valid_from": start, "valid_to": "2034-12-31",
                                      "continues_after_period": False, "weight_min_kg": "0", "weight_max_kg": None,
                                      "all_weights": True,
                                      "rate_per_kg": rate, "minimum_charge": "0", "evidence": refs}],
                    "fuel_terms": {"date_basis": "unknown", "charge_base": "unknown", "monthly_rates": [], "evidence": []},
                    "evidence": refs}
        x.responses["contract_pricing", x.contract_doc] = lambda req: prices(x.contract_doc, "4", req["requested_lane"])
        notice = x.add("Original rate notice BLUEJAY-C71/RN-1 for BLUEJAY-C71, NORTH-SOUTH.\n"
                       "Effective 2034-01-01 to 2034-12-31; all weights USD 6/kg, USD 0 minimum.",
                       "rate_notice", sender="Bo Example <bo@bluejay.example>")
        x.responses["document_identification", notice].update(contract_ids=["BLUEJAY-C71"], lanes=["NORTH-SOUTH"],
                                                              changes=["pricing"], effective_date="2034-01-01")
        x.responses["feedback_pricing_source", notice] = {"contract_id": "BLUEJAY-C71", "instrument_id": "BLUEJAY-C71/RN-1",
            "sent_by": "Bo Example", "sent_on": "2034-01-10", "binding_state": "unsigned", "evidence": x.refs(notice)}
        x.responses["contract_pricing", notice] = prices(notice, "6")
        fact = decision("close", "rate_accepted", fact={
            "type": "rate_accepted", "subject": {"contract_id": "BLUEJAY-C71", "lane": "NORTH-SOUTH"},
            "value": {"source": "rate notice", "sent_by": "Bo Example", "sent_on": "2034-01-10",
                      "effective_from": "2034-01-01", "effective_to": "2034-12-31"}, "evidence": "AP accepted this notice."},
            scope={"kind": "standing", "applies_to": {"contract_id": "BLUEJAY-C71", "lane": "NORTH-SOUTH"},
                   "valid_from": "2034-01-01", "valid_to": "2034-12-31"})
        return notice, fact, prices

    def test_accepted_notice_is_used_for_later_invoices_only_in_its_lane(self):
        x = self.fixture
        _, fact, _ = self.pricing_fixture()
        for identity, awb, amount, lane in (("BJ-100", "BJ-A1", "60", "NORTH-SOUTH"),
                                           ("BJ-200", "BJ-A2", "70", "NORTH-SOUTH"),
                                           ("BJ-300", "BJ-A3", "60", "NORTH-SOUTH"),
                                           ("BJ-400", "BJ-A4", "60", "WEST-EAST")):
            x.invoice([{"code": "X-FRT", "awb": awb, "quantity": "10", "quantity_unit": "kg",
                        "lane": lane, "billed_amount": amount}], invoice_id=identity)
        self.save_feedback(fact)
        report = x.run_report("2034-02-01")
        self.assertEqual(x.amounts(report, "overcharge"), {"BJ-200": "10.00", "BJ-400": "20.00"})
        learned = {item["invoice_id"]: item["learned_at"] for item in report["findings"]}
        self.assertIsNotNone(learned["BJ-200"])
        self.assertIsNone(learned["BJ-400"])

    def test_accepted_instrument_reference_resolves_its_parent_and_unattended_authority(self):
        x = self.fixture
        notice, fact, _ = self.pricing_fixture()
        x.responses["feedback_pricing_source", notice]["contract_id"] = "BLUEJAY-C71/RN-1"
        x.invoice([{"code": "X-FRT", "awb": "BJ-A2", "quantity": "10", "quantity_unit": "kg",
                    "lane": "NORTH-SOUTH", "billed_amount": "70"}], invoice_id="BJ-200")
        doc = x.add("Carrier picked up BJ-A2 on 2034-01-02.", "pickup_email")
        x.responses["document_identification", doc]["awbs"] = ["BJ-A2"]
        x.responses["pickup_record", doc] = {"awb": "BJ-A2", "pickup_date": "2034-01-02", "evidence": x.refs(doc)}
        self.save_feedback(fact)
        value = x.run_report("2034-02-01", mode="autonomous")
        finding = next(row for row in value["findings"] if row["invoice_id"] == "BJ-200")
        self.assertEqual(finding["amount_usd"], "10.00")
        self.assertEqual(finding["action"], "execute:dispute")
        self.assertEqual(finding["learned_at"]["feedback_item_id"], fact["item_id"])

    def test_missing_catalog_code_does_not_hide_explicit_freight_overcharge(self):
        x = self.fixture
        _, fact, _ = self.pricing_fixture()
        # Reproduce a partial contract catalog. Invoice meaning still identifies
        # freight, and the source-backed pricing extraction remains authoritative.
        x.policy["definitions"] = [entry for entry in x.policy["definitions"] if entry["code"] != "X-FRT"]
        for identity, awb, amount, lane in (("BJ-200", "BJ-A2", "70", "NORTH-SOUTH"),
                                           ("BJ-400", "BJ-A4", "60", "WEST-EAST")):
            x.invoice([{"code": "X-FRT", "awb": awb, "quantity": "10", "quantity_unit": "kg",
                        "lane": lane, "billed_amount": amount}], invoice_id=identity)
        for (task, _), response in x.responses.items():
            if task == "billing_lines":
                response["lines"][0].update(code=None, category="base_freight")
        self.assertEqual(x.amounts(x.run_report("2034-01-31"), "overcharge"), {"BJ-200": "30.00", "BJ-400": "20.00"})
        self.save_feedback(fact)
        report = x.run_report("2034-02-01")
        self.assertEqual(x.amounts(report, "overcharge"), {"BJ-200": "10.00", "BJ-400": "20.00"})
        learned = {item["invoice_id"]: item["learned_at"] for item in report["findings"]}
        self.assertIsNotNone(learned["BJ-200"])
        self.assertIsNone(learned["BJ-400"])

    def test_all_weight_pricing_with_unprinted_limits_preserves_feedback_lane_scope(self):
        x = self.fixture
        notice, fact, prices = self.pricing_fixture()
        def unlimited_prices(doc, rate, lane):
            value = prices(doc, rate, lane)
            value["freight_rates"][0]["weight_min_kg"] = None
            return value
        x.responses["contract_pricing", x.contract_doc] = lambda req: unlimited_prices(x.contract_doc, "4", req["requested_lane"])
        x.responses["contract_pricing", notice] = unlimited_prices(notice, "6", "NORTH-SOUTH")
        for identity, awb, amount, lane in (("BJ-200", "BJ-A2", "70", "NORTH-SOUTH"),
                                           ("BJ-400", "BJ-A4", "60", "WEST-EAST")):
            x.invoice([{"code": "X-FRT", "awb": awb, "quantity": "10", "quantity_unit": "kg",
                        "lane": lane, "billed_amount": amount}], invoice_id=identity)
        self.assertEqual(x.amounts(x.run_report("2034-01-31"), "overcharge"), {"BJ-200": "30.00", "BJ-400": "20.00"})
        self.save_feedback(fact)
        report = x.run_report("2034-02-01")
        self.assertEqual(x.amounts(report, "overcharge"), {"BJ-200": "10.00", "BJ-400": "20.00"})
        self.assertEqual(report["diagnostics"], [])
        with patch.object(h, "Anthropic", side_effect=AssertionError("All-weight prices must replay from cache")):
            self.assertEqual(h.report("2034-02-01", "supervised", str(x.state)), report)

    def test_explicit_precedence_and_later_signed_amendment(self):
        x = self.fixture
        notice, fact, prices = self.pricing_fixture()
        fact["decision"] = "resolve_conflict"
        fact["fact"]["type"] = "precedence"
        fact["fact"]["value"] = {"use": "amendment", "amendment": "BLUEJAY-C71/RN-1"}
        x.invoice([{"code": "X-FRT", "awb": "BJ-A2", "pickup_date": "2034-03-01", "quantity": "10",
                    "quantity_unit": "kg", "billed_amount": "70"}])
        self.save_feedback(fact)
        self.assertEqual(x.amounts(x.run_report("2034-02-01"), "overcharge"), {"BJ-100": "10.00"})
        amendment = x.add("BLUEJAY-C71/AMD-2 for BLUEJAY-C71, NORTH-SOUTH. Effective 2034-02-05 to 2034-12-31.\n"
                          "All weights USD 5/kg, minimum USD 0. Signed by both parties on 2034-02-05.",
                          "amendment", received="2034-02-05", attachment="signed.pdf")
        x.responses["document_identification", amendment].update(contract_ids=["BLUEJAY-C71"], lanes=["NORTH-SOUTH"],
                                                                  changes=["pricing"], effective_date="2034-02-05")
        x.responses["contract_terms", amendment] = {**x.terms, "effective_date": "2034-02-05", "contacts": [],
                                                    "evidence": x.refs(amendment)}
        x.responses["feedback_pricing_source", amendment] = {"contract_id": "BLUEJAY-C71", "instrument_id": "BLUEJAY-C71/AMD-2",
            "sent_by": None, "sent_on": "2034-02-05", "binding_state": "executed", "evidence": x.refs(amendment)}
        x.responses["contract_pricing", amendment] = prices(amendment, "5", start="2034-02-05")
        report = x.run_report("2034-03-01")
        self.assertEqual(x.amounts(report, "overcharge"), {"BJ-100": "20.00"})
        self.assertIsNone(report["findings"][0]["learned_at"])

    def test_feedback_close_is_persistent_and_key_specific(self):
        x = self.fixture
        x.invoice([{"code": "X-ACT", "awb": None, "service_month": "2034-01", "billed_amount": "15"}])
        x.invoice([{"code": "X-ACT", "awb": None, "service_month": "2034-02", "billed_amount": "15"}],
                  invoice_id="BJ-200", invoice_date="2034-02-05", received="2034-02-05")
        self.save_feedback(decision())
        self.assertEqual(x.amounts(x.run_report("2034-01-31"), "overcharge"), {"BJ-100": "5.00"})
        self.assertEqual(x.amounts(x.run_report("2034-02-05"), "overcharge"), {"BJ-200": "5.00"})
        with patch.object(h, "Anthropic", side_effect=AssertionError("Unexpected API")):
            self.assertEqual(x.amounts(h.report("2034-02-05", "supervised", str(x.state)), "overcharge"), {"BJ-200": "5.00"})

    def test_override_updates_totals_learning_and_payment_principal(self):
        x = self.fixture
        x.invoice([{"code": "X-ACT", "awb": None, "service_month": "2034-01", "billed_amount": "110"}], due="2034-02-10")
        x.record("payments", [payment(principal="110", open_amount="110", due_date="2034-02-10")])
        item = decision("override_amount")
        item["fact"]["value"]["amount_usd"] = "10.00"
        self.save_feedback(item)
        report = x.run_report("2034-02-01")
        self.assertEqual(report["findings"][0]["amount_usd"], "10.00")
        self.assertEqual(report["findings"][0]["learned_at"], {"feedback_item_id": item["item_id"], "batch": "batch_01"})
        self.assertEqual(report["portfolio"]["late_fee_risk"][0]["amount_usd"], "2.00")
        self.assertEqual(report["totals"]["total_overpayment"], "10.00")

    def test_closed_dispute_no_longer_reduces_payment_risk_principal(self):
        x = self.fixture
        x.invoice([{"code": "X-ACT", "awb": None, "service_month": "2034-01", "billed_amount": "110"}], due="2034-02-10")
        x.record("payments", [payment(principal="110", open_amount="110", due_date="2034-02-10")])
        self.save_feedback(decision())
        report = x.run_report("2034-02-01")
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["portfolio"]["late_fee_risk"][0]["amount_usd"], "2.20")

    def authority_item(self, person="Ada Example"):
        return decision("confirm", "sender_authority", finding_key={"invoice_id": "BJ-100", "class": "waived_but_billed"},
                        fact={"type": "sender_authority", "subject": {"person": person, "party": "Bluejay"},
                              "value": {"categories": ["waivers"]}, "evidence": "AP reviewed the delegation letter."},
                        scope={"kind": "standing", "applies_to": {"contract_id": "BLUEJAY-C71", "person": person, "category": "waivers"},
                               "valid_from": "2034-01-01", "valid_to": "2034-02-28"})

    def test_sender_authority_has_person_category_contract_and_date_limits(self):
        x = self.fixture
        x.invoice([{"code": "X-DEL", "billed_amount": "20"}])
        x.operations(delivery="carrier")
        x.record("waivers", [waiver()], sender="Ada Example <ada@bluejay.example>")
        self.assertEqual(x.run_report("2034-01-31")["findings"], [])
        self.save_feedback(self.authority_item())
        report = x.run_report("2034-02-01")
        item = next(item for item in report["findings"] if item["class"] == "waived_but_billed")
        self.assertEqual(item["amount_usd"], "20.00")
        self.assertIsNotNone(item["learned_at"])
        fact = f.load(x.state, "2034-02-01")[0]
        for values, on_date in (({"contract_id": "OTHER", "person": "Ada Example", "category": "waivers"}, "2034-01-02"),
                                ({"contract_id": "BLUEJAY-C71", "person": "Bob Example", "category": "waivers"}, "2034-01-02"),
                                ({"contract_id": "BLUEJAY-C71", "person": "Ada Example", "category": "rates"}, "2034-01-02"),
                                ({"contract_id": "BLUEJAY-C71", "person": "Ada Example", "category": "waivers"}, "2034-03-01")):
            self.assertFalse(f.matches(fact, values, on_date))

    def test_authority_date_is_when_sender_wrote_not_earlier_pickup(self):
        x = self.fixture
        x.invoice([{"code": "X-DEL", "billed_amount": "20"}])
        x.operations(delivery="carrier")
        x.record("waivers", [waiver()], sender="Ada Example <ada@bluejay.example>", received="2034-01-10")
        item = self.authority_item()
        item["scope"]["valid_from"] = "2034-01-05"
        self.save_feedback(item)
        self.assertEqual(x.amounts(x.run_report("2034-02-01"), "waived_but_billed"), {"BJ-100": "20.00"})

    def test_one_off_month_approval_never_spills_to_another_invoice(self):
        x = self.fixture
        x.invoice([{"code": "X-ACT", "awb": None, "service_month": "2034-01", "billed_amount": "10"}])
        x.invoice([{"code": "X-ACT", "awb": None, "service_month": "2034-02", "billed_amount": "10"}],
                  invoice_id="BJ-200", invoice_date="2034-02-05", received="2034-02-05")
        x.record("waivers", [waiver(awbs=[], codes=["X-ACT"], month="2034-01")], sender="Ada Example <ada@bluejay.example>")
        item = decision("one_off_approval", "waiver_scope",
                        finding_key={"invoice_id": "BJ-100", "class": "waived_but_billed"},
                        fact={"type": "waiver_scope", "subject": {"person": "Ada Example", "party": "Bluejay"},
                              "value": {"months": ["2034-01"]}, "evidence": "January only was approved."})
        self.save_feedback(item)
        report = x.run_report("2034-02-05")
        self.assertEqual(x.amounts(report, "waived_but_billed"), {"BJ-100": "10.00"})
        self.assertIsNotNone(next(item for item in report["findings"] if item["class"] == "waived_but_billed")["learned_at"])


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "batch_01").mkdir()
        (self.root / "batch_01/documents.json").write_text("[]")
        self.file = self.root / "input.json"

    def save(self, value):
        self.file.write_text(json.dumps(value))
        f.store(self.file, self.root)

    def test_idempotent_ingest_and_date_cutoff(self):
        value = envelope([decision()])
        self.save(value)
        before = (self.root / "feedback.json").read_bytes()
        self.save(value)
        self.assertEqual((self.root / "feedback.json").read_bytes(), before)
        self.assertEqual(f.load(self.root, "2034-01-31"), [])
        self.assertEqual(len(f.load(self.root, "2034-02-01")), 1)

    def test_invalid_input_never_overwrites_prior_memory(self):
        value = envelope([decision()])
        self.save(value)
        before = (self.root / "feedback.json").read_bytes()
        value["items"][0]["scope"]["applies_to"]["unknown_filter"] = "unsafe"
        with self.assertRaises(ValueError):
            self.save(value)
        self.assertEqual((self.root / "feedback.json").read_bytes(), before)

    def test_reused_id_with_different_decision_is_rejected(self):
        value = envelope([decision()])
        self.save(value)
        value["items"][0]["decision"] = "reject"
        with self.assertRaises(ValueError):
            self.save(value)

    def test_feedback_requires_ingested_batch_and_aware_timestamp(self):
        value = envelope([decision()])
        value["after_batch"] = "batch_02"
        with self.assertRaises(ValueError):
            self.save(value)
        value["after_batch"] = "batch_01"
        value["decided_at"] = "2034-02-01T12:00:00"
        with self.assertRaises(ValueError):
            self.save(value)

    def test_saved_source_tampering_is_detected(self):
        self.save(envelope([decision()]))
        path = self.root / "feedback.json"
        memory = json.loads(path.read_text())
        memory["files"][0]["feedback"]["items"][0]["decision"] = "reject"
        path.write_text(json.dumps(memory))
        with self.assertRaises(ValueError):
            f.load(self.root, "2034-02-01")

    def test_real_cli_persists_feedback_without_an_api_call(self):
        self.file.write_text(json.dumps(envelope([decision()])))
        command = [sys.executable, str(Path(h.__file__).resolve()), "feedback", str(self.file), "--state-dir", str(self.root)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(f.load(self.root, "2034-02-01")[0]["item_id"], "own-feedback-1")

    def test_practice_feedback_file_uses_the_supported_format(self):
        original = Path(h.__file__).resolve().parents[1] / "data/scenarios/feedback/W1_batch_01.json"
        f.validate(json.loads(original.read_text()))


if __name__ == "__main__":
    unittest.main()

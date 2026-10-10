"""Source-checked authority, conflict arithmetic and persistent revision tests."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness import harness as h
import test_billing as fixtures
from test_cashback_feedback import decision, envelope
from harness import feedback
from harness import authority
from harness import workflow as w
from grader.validate import citation_errors, schema_errors


class AuthorityHistoryTests(unittest.TestCase):
    def test_slashes_in_real_contract_ids_are_preserved_and_longest_held_parent_wins(self):
        self.assertEqual(w.governing_contract("C/71/RN-3", ["C", "C/71"]), "C/71")
        self.assertEqual(w.governing_contract("C/71", ["C/71"]), "C/71")
        self.assertEqual(w.governing_contract("C/710/RN-3", ["C/71"]), "C/710/RN-3")
        document = {"doc": "batch/notice.eml", "pages": ["Reference: C/71/RN-3. Rates for NORTH-SOUTH."]}
        self.assertTrue(authority.targets_contract(document, "C/71", {"contract_ids": ["C/71/RN-3"]}))
        self.assertTrue(authority.printed_parent(document, "C/71"))
        self.assertFalse(authority.printed_parent(document, "C/7"))

    def test_printed_parent_reference_routes_an_omitted_classification_id_without_prefix_collisions(self):
        document = {"doc": "batch/anything.eml", "pages": ["Reference: C-71/RN-3. Rates for NORTH-SOUTH."]}
        self.assertTrue(authority.targets_contract(document, "C-71", {"contract_ids": []}))
        self.assertTrue(authority.targets_contract(document, "C-71", {"contract_ids": ["C-71/RN-3"]}))
        self.assertFalse(authority.targets_contract(document, "C-7", {"contract_ids": []}))
        self.assertFalse(authority.targets_contract(document, "C-71", {"contract_ids": ["OTHER-C71"]}))
        document["doc"] = "batch/C-71-RN-3.pdf"
        document["pages"] = ["Rates for NORTH-SOUTH, no agreement reference."]
        self.assertFalse(authority.targets_contract(document, "C-71", {"contract_ids": []}))
    def setup_pricing(self, priority="unranked", extra="", currency="USD", freight_clause=None):
        x = fixtures.BillingPipelineTests(methodName="runTest")
        clauses = {"unranked": "An authorised rate notice and a signed amendment are unranked; neither prevails automatically.",
                   "amendment": "A signed amendment takes precedence over an authorised rate notice.",
                   "notice": "An authorised rate notice takes precedence over a signed amendment."}
        freight_clause = freight_clause or "X-FRT: Air freight USD 4/kg, all weights, minimum USD 0 on every lane for 2034."
        x.cashback_clause = ("Schedule 1: Fiona Reed <fiona@bluejay.example> is also the Carrier's rates contact.\n"
                             + freight_clause + "\n"
                             "X-FUEL: Fuel surcharge is 10 percent of correct base freight on pickup dates in January 2034.\n"
                             + clauses[priority] + "\n" + extra)
        x.setUp()
        x.price_currency = currency
        self.addCleanup(x.tearDown)
        refs = x.refs(x.contract_doc)
        x.terms["contacts"].append({"category": "rates", "party": "carrier", "person": "Fiona Reed",
                                     "email": "fiona@bluejay.example", "evidence": refs})
        for code, category, label in (("X-FRT", "base_freight", "Air freight"), ("X-FUEL", "fuel", "Fuel surcharge")):
            x.definitions.append((code, category, label))
            x.policy["definitions"].append({"code": code, "category": category, "labels": [label], "evidence": refs})
        x.responses["pricing_precedence", x.contract_doc] = {"notice_vs_amendment": priority, "evidence": refs}
        def prices(doc, rate, lane):
            legacy = [{"source_page": 1, "source_quote": x.responses["source", doc]["pages"][0]}]
            return {"contract_id": "BLUEJAY-C71", "effective_date": "2034-01-01", "expiry_date": None,
                    "pricing_date_basis": "pickup_date", "freight_rates": [{"lane": lane, "currency": currency,
                    "valid_from": None, "valid_to": None, "all_weights": True, "weight_min_kg": None,
                    "weight_max_kg": None, "rate_per_kg": rate, "minimum_charge": "0",
                    "continues_after_period": False, "evidence": legacy}],
                    "fuel_terms": {"date_basis": "pickup_date", "charge_base": "base_freight_only",
                                   "monthly_rates": [{"month": "2034-01", "percentage": "10", "evidence": legacy}],
                                   "evidence": legacy}, "evidence": legacy}
        x.responses["document_identification", x.contract_doc]["effective_date"] = "2034-01-01"
        x.responses["contract_pricing", x.contract_doc] = lambda req: prices(x.contract_doc, "4", req["requested_lane"])
        return x, prices

    def instrument(self, x, prices, rate, *, kind="amendment", received="2034-01-11", sender="Fiona Reed <fiona@bluejay.example>",
                   status="original", signed=True, lane="NORTH-SOUTH", effective="2034-01-01", reference="AMD-1", extra=""):
        text = (f"Instrument BLUEJAY-C71/{reference} for BLUEJAY-C71, lane {lane}. Effective {effective}.\n"
                f"Air freight {x.price_currency} {rate}/kg for all weights, minimum {x.price_currency} 0; all other terms unchanged.\n" + extra)
        if kind == "amendment":
            text += (f"Signed for Carrier: Fiona Reed; signed for Shipper: Ken Shah, {received}." if signed else "Unsigned draft; signature placeholders only.")
        doc = x.add(text, kind, received=received, sender=sender, status=status,
                    attachment=f"{reference}.pdf" if kind == "amendment" else None)
        x.responses["document_identification", doc].update(contract_ids=["BLUEJAY-C71"], lanes=[lane],
                                                           changes=["pricing"], effective_date=effective)
        if kind == "amendment":
            x.responses["contract_terms", doc] = {**x.terms, "binding_state": "executed" if signed else "draft",
                "changes_term": False, "effective_date": effective, "expiry_date": None, "contacts": [], "evidence": x.refs(doc)}
        x.responses["feedback_pricing_source", doc] = {"contract_id": "BLUEJAY-C71", "instrument_id": f"BLUEJAY-C71/{reference}",
                "sent_by": "Fiona Reed" if sender.startswith("Fiona") else "Other Sender", "sent_on": received,
                "binding_state": "executed" if kind == "amendment" and signed else "unsigned", "evidence": x.refs(doc)}
        def price_request(req):
            result = prices(doc, rate, req["requested_lane"])
            result["effective_date"] = effective
            result["fuel_terms"] = {"date_basis": "unknown", "charge_base": "unknown", "monthly_rates": [], "evidence": []}
            return result
        x.responses["contract_pricing", doc] = price_request
        x.responses["billing_policy", doc] = {"contract_id": "BLUEJAY-C71", "carrier_name": None,
            "definitions": [], "rules": [], "fx_annex_name": None, "fx_rows": [], "evidence": x.refs(doc),
            "payment": {key: [] if key == "evidence" else None for key in x.policy["payment"]}}
        return doc

    def invoice(self, x, billed="60", invoice="BJ-100", lane="NORTH-SOUTH", currency="USD"):
        fuel = str(h.calculate_percentage_amount(h.calculate_line_amount(billed, "1"), "10"))
        return x.invoice([{"code": "X-FRT", "awb": "BJ-A1", "quantity": "10", "quantity_unit": "kg", "lane": lane,
                           "billed_amount": billed},
                          {"code": "X-FUEL", "awb": "BJ-A1", "quantity": billed, "rate": "10", "rate_kind": "percentage",
                           "quantity_unit": None, "lane": lane, "billed_amount": fuel}], invoice_id=invoice, currency=currency)

    def test_unranked_binding_sources_include_fuel_in_conflict_range_and_exclude_totals(self):
        x, prices = self.setup_pricing()
        amendment = self.instrument(x, prices, "5")
        notice = self.instrument(x, prices, "6", kind="rate_notice", received="2034-01-12", reference="RN-1")
        self.invoice(x)
        report = x.run_report("2034-01-12")
        item = report["findings"][0]
        self.assertEqual((item["verdict"], item["action"], item["amount_range"]), ("conflict", "escalate", ["0.00", "11.00"]))
        self.assertIsNone(item["amount_usd"])
        self.assertEqual(report["totals"]["total_savings"], "0.00")
        self.assertTrue({amendment, notice, x.contract_doc}.issubset({ref["doc"] for ref in item["evidence"]}))
        self.assertEqual(x.run_report("2034-01-12", "autonomous")["findings"][0]["action"], "escalate")

    def test_execution_uses_the_existing_contract_fact_and_notice_reference_can_supply_the_parent(self):
        x, prices = self.setup_pricing()
        amendment = self.instrument(x, prices, "5")
        x.responses["feedback_pricing_source", amendment]["binding_state"] = "unknown"
        notice = self.instrument(x, prices, "6", kind="rate_notice", received="2034-01-12", reference="RN-1")
        x.responses["document_identification", notice]["contract_ids"] = []
        self.invoice(x)
        item = x.run_report("2034-01-12")["findings"][0]
        self.assertEqual((item["verdict"], item["amount_range"]), ("conflict", ["0.00", "11.00"]))
        self.assertTrue({amendment, notice}.issubset({ref["doc"] for ref in item["evidence"]}))

    def test_unknown_unrelated_delivery_does_not_hide_a_complete_freight_fuel_conflict(self):
        x, prices = self.setup_pricing()
        self.instrument(x, prices, "5")
        self.instrument(x, prices, "6", kind="rate_notice", received="2034-01-12", reference="RN-1")
        x.invoice([{"code": "X-FRT", "quantity": "10", "quantity_unit": "kg", "billed_amount": "60"},
                   {"code": "X-FUEL", "quantity": "60", "rate": "10", "rate_kind": "percentage", "billed_amount": "6"},
                   {"code": "X-DEL", "quantity": "1", "quantity_unit": "shpt", "billed_amount": "20"}])
        report = x.run_report("2034-01-12")
        item = report["findings"][0]
        self.assertEqual((item["verdict"], item["amount_range"]), ("conflict", ["0.00", "11.00"]))
        self.assertEqual(item["conflict_scope"]["line_indices"], [1, 2])
        self.assertEqual(item["conflict_scope"]["unpriced_line_indices"], [3])
        self.assertEqual(report["diagnostics"][0]["line_index"], 3)
        self.assertEqual(report["totals"]["total_savings"], "0.00")

    def test_rate_only_instruments_inherit_the_parent_date_rule_and_keep_cached_facts_unchanged(self):
        x, prices = self.setup_pricing()
        for kind, rate, reference in (("amendment", "5", "AMD-1"), ("rate_notice", "6", "RN-1")):
            doc = self.instrument(x, prices, rate, kind=kind, reference=reference)
            original = x.responses["contract_pricing", doc]
            def unstated(req, original=original, reference=reference):
                value = original(req)
                value.update(pricing_date_basis="unknown", contract_id=f"BLUEJAY-C71/{reference}")
                return value
            x.responses["contract_pricing", doc] = unstated
        self.invoice(x)
        report = x.run_report("2034-01-12")
        self.assertEqual(report["findings"][0]["amount_range"], ["0.00", "11.00"])
        cached = [json.loads(path.read_text()) for path in (x.state / "extractions").glob("*.json")]
        instruments = [value for value in cached if value.get("contract_id", "").startswith("BLUEJAY-C71/")
                       and "pricing_date_basis" in value]
        self.assertEqual(len(instruments), 2)
        self.assertTrue(all(value["pricing_date_basis"] == "unknown" for value in instruments))

    def test_an_explicit_instrument_date_rule_is_preserved_and_an_unknown_parent_is_not_guessed(self):
        for parent_basis, instrument_basis, expected in (("pickup_date", "invoice_date", "invoice_date"),
                                                         ("unknown", "unknown", "unknown")):
            with self.subTest(parent=parent_basis, instrument=instrument_basis):
                x, prices = self.setup_pricing()
                doc = self.instrument(x, prices, "5")
                documents = {key: x.responses["source", key] for key in (x.contract_doc, doc)}
                agreement = {"document": documents[x.contract_doc], "terms": x.terms}
                raw = {key: x.responses["contract_pricing", key]({"requested_lane": "NORTH-SOUTH"})
                       for key in (x.contract_doc, doc)}
                raw[x.contract_doc]["pricing_date_basis"] = parent_basis
                raw[doc]["pricing_date_basis"] = instrument_basis
                saved = copy.deepcopy(raw)
                context = {"backend": h, "state_dir": str(x.state), "metadata": {
                    key: x.responses["document_identification", key] for key in raw}}
                with patch.object(h, "cached_extract_contract", side_effect=lambda source, lane, state: raw[source["doc"]]):
                    resolved = authority.contract_prices(documents[doc], agreement, "NORTH-SOUTH", context)
                self.assertEqual(resolved["pricing_date_basis"], expected)
                self.assertEqual(raw, saved)

    def test_unknown_dependent_fuel_prevents_an_invented_freight_only_range(self):
        x, prices = self.setup_pricing()
        root_prices = x.responses["contract_pricing", x.contract_doc]
        def no_fuel(req):
            result = root_prices(req)
            result["fuel_terms"]["monthly_rates"] = []
            return result
        x.responses["contract_pricing", x.contract_doc] = no_fuel
        self.instrument(x, prices, "5")
        self.instrument(x, prices, "6", kind="rate_notice", received="2034-01-12", reference="RN-1")
        self.invoice(x)
        report = x.run_report("2034-01-12")
        self.assertEqual(report["findings"], [])
        self.assertTrue(report["diagnostics"])

    def test_contract_precedence_ranks_sources_in_either_direction(self):
        for ranking, amount in (("amendment", "11.00"), ("notice", None)):
            with self.subTest(ranking=ranking):
                x, prices = self.setup_pricing(ranking)
                self.instrument(x, prices, "5")
                self.instrument(x, prices, "6", kind="rate_notice", received="2034-01-12", reference="RN-1")
                self.invoice(x)
                self.assertEqual(x.amounts(x.run_report("2034-01-12"), "overcharge"), {"BJ-100": amount} if amount else {})

    def test_unauthorised_quoted_draft_other_lane_and_future_instruments_do_not_override(self):
        for kwargs in ({"kind": "rate_notice", "sender": "Other Sender <other@bluejay.example>"},
                       {"kind": "rate_notice", "status": "quoted_only"}, {"signed": False},
                       {"lane": "WEST-EAST"}, {"effective": "2034-03-01"}):
            with self.subTest(kwargs=kwargs):
                x, prices = self.setup_pricing()
                self.instrument(x, prices, "6", **kwargs)
                self.invoice(x)
                self.assertEqual(x.amounts(x.run_report("2034-01-12"), "overcharge"), {"BJ-100": "22.00"})

    def test_new_backdated_amendment_supersedes_with_exact_historical_replay(self):
        x, prices = self.setup_pricing()
        self.invoice(x)
        first = x.run_report("2034-01-10")
        amendment = self.instrument(x, prices, "5")
        second = x.run_report("2034-01-11")
        item = second["findings"][0]
        self.assertEqual(item["amount_usd"], "11.00")
        self.assertEqual(item["supersedes"], [first["findings"][0]["id"]])
        self.assertNotEqual(item["id"], first["findings"][0]["id"])
        self.assertIn(amendment, {ref["doc"] for ref in item["evidence"]})
        with patch.object(h, "Anthropic", side_effect=AssertionError("Historical replay must not call the API")):
            self.assertEqual(h.report("2034-01-10", "supervised", str(x.state)), first)
            self.assertEqual(h.report("2034-01-11", "supervised", str(x.state)), second)

    def test_new_retro_key_has_no_supersedes_and_a_cleared_key_has_a_retraction(self):
        x, prices = self.setup_pricing()
        self.invoice(x, "40")
        self.assertEqual(x.run_report("2034-01-10")["findings"], [])
        self.instrument(x, prices, "3")
        report = x.run_report("2034-01-11")
        prior = report["findings"][0]
        self.assertEqual((prior["amount_usd"], prior["retro"], prior["supersedes"]), ("11.00", True, []))
        cleared_by = self.instrument(x, prices, "4", received="2034-01-12", reference="AMD-2")
        cleared = x.run_report("2034-01-12")["findings"][0]
        self.assertEqual((cleared["verdict"], cleared["action"], cleared["supersedes"]), ("no_finding", "none", [prior["id"]]))
        self.assertIn(cleared_by, {ref["doc"] for ref in cleared["evidence"]})

    def test_feedback_resolves_conflict_and_carries_both_provenance_and_supersedes(self):
        x, prices = self.setup_pricing()
        self.instrument(x, prices, "5")
        self.instrument(x, prices, "6", kind="rate_notice", received="2034-01-12", reference="RN-1")
        self.invoice(x)
        before = x.run_report("2034-02-01")
        fact = decision("resolve_conflict", "precedence", fact={"type": "precedence", "subject": {"contract_id": "BLUEJAY-C71"},
            "value": {"use": "amendment", "amendment": "BLUEJAY-C71/AMD-1"}, "evidence": "AP selected the executed amendment."},
            scope={"kind": "standing", "applies_to": {"contract_id": "BLUEJAY-C71", "lane": "NORTH-SOUTH"},
                   "valid_from": "2034-01-01", "valid_to": None})
        path = x.root / "feedback-input.json"
        path.write_text(json.dumps(envelope([fact], "2034-02-02T12:00:00Z")))
        feedback.store(str(path), str(x.state))
        after = x.run_report("2034-02-02")
        item = after["findings"][0]
        self.assertEqual((item["verdict"], item["amount_usd"]), ("finding", "11.00"))
        self.assertEqual(item["learned_at"]["feedback_item_id"], fact["item_id"])
        self.assertEqual(item["supersedes"], [before["findings"][0]["id"]])

    def test_same_amount_from_two_binding_sources_is_not_a_monetary_conflict(self):
        x, prices = self.setup_pricing()
        self.instrument(x, prices, "5")
        self.instrument(x, prices, "5", kind="rate_notice", received="2034-01-12", reference="RN-1")
        self.invoice(x)
        item = x.run_report("2034-01-12")["findings"][0]
        self.assertEqual((item["verdict"], item["amount_usd"]), ("finding", "11.00"))

    def test_missing_named_price_annex_requests_rates_holder_without_inventing_money(self):
        x, prices = self.setup_pricing(freight_clause="X-FRT: Air freight prices for NORTH-SOUTH are specified only in Annex R revision 2.")
        x.responses["contract_pricing", x.contract_doc] = lambda req: {**prices(x.contract_doc, "4", req["requested_lane"]), "freight_rates": []}
        x.responses["pricing_dependencies", x.contract_doc] = {"dependencies": [{"name": "Annex R revision 2", "category": "base_freight",
            "lanes": ["NORTH-SOUTH"], "codes": ["X-FRT"], "valid_from": "2034-01-01", "valid_to": None, "evidence": x.refs(x.contract_doc)}]}
        self.invoice(x)
        report = x.run_report("2034-01-10")
        item = report["findings"][0]
        self.assertEqual((item["verdict"], item["action"], item["amount_usd"]), ("cannot_determine", "draft:request_document", None))
        self.assertEqual(item["missing_doc"]["name"], "Annex R revision 2")
        self.assertEqual(item["missing_doc"]["holder"]["person"], "Fiona Reed")
        self.assertEqual(report["totals"]["total_savings"], "0.00")
        self.assertEqual(report["diagnostics"], [])

    def test_standing_sender_approval_does_not_override_amendment_precedence(self):
        x, prices = self.setup_pricing("amendment")
        self.instrument(x, prices, "5")
        self.instrument(x, prices, "6", kind="rate_notice", received="2034-01-12", reference="RN-1",
                        sender="Other Sender <other@bluejay.example>")
        self.invoice(x)
        fact = decision("confirm", "sender", fact={"type": "sender_authority", "subject": {"person": "Other Sender", "email": "other@bluejay.example"},
            "value": {"categories": ["rates"]}, "evidence": "AP confirmed this original sender can publish rates."},
            scope={"kind": "standing", "applies_to": {"contract_id": "BLUEJAY-C71", "person": "Other Sender", "category": "rates"},
                   "valid_from": "2034-01-01", "valid_to": None})
        path = x.root / "feedback.json"
        path.write_text(json.dumps(envelope([fact], "2034-01-13T12:00:00Z")))
        h.ingest(str(x.batch), str(x.state))
        feedback.store(str(path), str(x.state))
        item = x.run_report("2034-01-13")["findings"][0]
        self.assertEqual((item["verdict"], item["amount_usd"]), ("finding", "11.00"))

    def test_unchanged_key_has_stable_id_after_unrelated_documents_and_mode_isolation(self):
        x, _ = self.setup_pricing()
        self.invoice(x)
        first = x.run_report("2034-01-10")
        x.add("An unrelated administrative FYI, with no pricing or term change.", received="2034-01-11")
        second = x.run_report("2034-01-11")
        self.assertEqual(first["findings"][0]["id"], second["findings"][0]["id"])
        autonomous = x.run_report("2034-01-11", "autonomous")
        self.assertEqual(autonomous["findings"][0]["supersedes"], [])
        self.assertNotEqual(autonomous["findings"][0]["id"], second["findings"][0]["id"])

    def test_later_report_first_cannot_leak_ids_or_sources_into_an_earlier_cutoff(self):
        x, prices = self.setup_pricing()
        self.invoice(x)
        amendment = self.instrument(x, prices, "5")
        later = x.run_report("2034-01-11")
        early = x.run_report("2034-01-10")
        self.assertEqual(early["findings"][0]["amount_usd"], "22.00")
        self.assertEqual(early["findings"][0]["supersedes"], [])
        self.assertNotIn(amendment, {ref["doc"] for ref in early["findings"][0]["evidence"]})
        self.assertEqual(x.run_report("2034-01-11"), later)

    def test_unpriced_row_is_a_diagnostic_never_a_false_retraction(self):
        x, prices = self.setup_pricing()
        self.invoice(x)
        x.run_report("2034-01-10")
        self.instrument(x, prices, "5")
        # A newly held operational record makes required storage entitlement
        # unknown. An unrelated new original must not imply that freight was cleared.
        x.add("New original operational information for BLUEJAY-C71, not a price correction.", received="2034-01-11")
        with patch("harness.billing._price_invoice", side_effect=lambda record, context, policies:
                   context["diagnostics"].append({"invoice_id": "BJ-100", "reason": "Required entitlement is unknown."})):
            report = x.run_report("2034-01-11")
        self.assertEqual(report["findings"], [])
        self.assertTrue(report["diagnostics"])

    def test_snapshot_integrity_failure_does_not_overwrite_saved_history(self):
        x, _ = self.setup_pricing()
        self.invoice(x)
        x.run_report("2034-01-10")
        path = x.state / "report_history.json"
        memory = json.loads(path.read_text())
        memory["snapshots"][0]["report"]["findings"][0]["amount_usd"] = "99.00"
        path.write_text(json.dumps(memory))
        corrupted = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "integrity check"):
            x.run_report("2034-01-10")
        self.assertEqual(path.read_bytes(), corrupted)

    def test_clearing_feedback_has_a_revision_and_provenance_without_savings(self):
        x, _ = self.setup_pricing()
        self.invoice(x)
        first = x.run_report("2034-01-10")
        fact = decision(item_id="close-old")
        path = x.root / "feedback-input.json"
        path.write_text(json.dumps(envelope([fact], "2034-02-01T12:00:00Z")))
        feedback.store(str(path), str(x.state))
        report = x.run_report("2034-02-01")
        item = report["findings"][0]
        self.assertEqual((item["verdict"], item["action"], item["supersedes"]), ("no_finding", "none", [first["findings"][0]["id"]]))
        self.assertEqual(item["learned_at"]["feedback_item_id"], fact["item_id"])
        self.assertEqual(report["totals"]["total_savings"], "0.00")

    def test_returning_to_an_earlier_amount_creates_a_new_revision_without_a_cycle(self):
        x, prices = self.setup_pricing()
        self.invoice(x)
        first = x.run_report("2034-01-10")["findings"][0]
        self.instrument(x, prices, "5")
        second = x.run_report("2034-01-11")["findings"][0]
        self.instrument(x, prices, "4", received="2034-01-12", reference="AMD-2")
        third = x.run_report("2034-01-12")["findings"][0]
        self.assertEqual(third["amount_usd"], first["amount_usd"])
        self.assertEqual(third["supersedes"], [second["id"]])
        self.assertEqual(len({first["id"], second["id"], third["id"]}), 3)

    def test_same_day_new_arrival_supersedes_and_identical_replay_does_not_append_snapshots(self):
        x, prices = self.setup_pricing()
        self.invoice(x)
        first = x.run_report("2034-01-12")["findings"][0]
        self.instrument(x, prices, "5")
        second = x.run_report("2034-01-12")
        self.assertEqual(second["findings"][0]["supersedes"], [first["id"]])
        path = x.state / "report_history.json"
        before = path.read_bytes()
        self.assertEqual(x.run_report("2034-01-12"), second)
        self.assertEqual(path.read_bytes(), before)

    def test_conflict_bounds_convert_the_whole_invoice_once_using_invoice_month_fx(self):
        x, prices = self.setup_pricing(currency="INR", freight_clause="X-FRT: Air freight INR 400/kg, all weights, minimum INR 0.")
        self.instrument(x, prices, "500")
        self.instrument(x, prices, "600", kind="rate_notice", received="2034-01-12", reference="RN-1")
        self.invoice(x, billed="6000", currency="INR")
        item = x.run_report("2034-01-12")["findings"][0]
        self.assertEqual((item["verdict"], item["amount_range"]), ("conflict", ["0.00", "11.00"]))

    def test_later_backdated_amendment_beats_an_older_amendment_with_a_later_start(self):
        x, prices = self.setup_pricing()
        self.instrument(x, prices, "5", received="2034-01-06", effective="2034-01-02")
        self.invoice(x)
        first = x.run_report("2034-01-10")["findings"][0]
        self.assertEqual(first["amount_usd"], "11.00")
        later = self.instrument(x, prices, "3", received="2034-01-11", effective="2034-01-01", reference="AMD-2")
        item = x.run_report("2034-01-11")["findings"][0]
        self.assertEqual(item["amount_usd"], "33.00")
        self.assertEqual(item["supersedes"], [first["id"]])
        self.assertIn(later, {ref["doc"] for ref in item["evidence"]})

    def test_independent_lane_conflicts_do_not_cancel_at_the_invoice_total(self):
        x, prices = self.setup_pricing()
        amendment = self.instrument(x, prices, "5", lane="NORTH-SOUTH and WEST-EAST", extra="For WEST-EAST the freight rate is USD 6/kg.\n")
        notice = self.instrument(x, prices, "6", kind="rate_notice", received="2034-01-12", reference="RN-1",
                                 lane="NORTH-SOUTH and WEST-EAST", extra="For WEST-EAST the freight rate is USD 5/kg.\n")
        for doc, rates in ((amendment, {"NORTH-SOUTH": "5", "WEST-EAST": "6"}), (notice, {"NORTH-SOUTH": "6", "WEST-EAST": "5"})):
            x.responses["document_identification", doc]["lanes"] = list(rates)
            def pricing(req, doc=doc, rates=rates):
                result = prices(doc, rates[req["requested_lane"]], req["requested_lane"])
                result["fuel_terms"]["monthly_rates"] = []
                return result
            x.responses["contract_pricing", doc] = pricing
        rows = []
        for lane, awb in (("NORTH-SOUTH", "BJ-A1"), ("WEST-EAST", "BJ-A2")):
            rows += [{"code": "X-FRT", "awb": awb, "quantity": "10", "quantity_unit": "kg", "lane": lane, "billed_amount": "60"},
                     {"code": "X-FUEL", "awb": awb, "quantity": "60", "rate": "10", "rate_kind": "percentage", "quantity_unit": None,
                      "lane": lane, "billed_amount": "6"}]
        x.invoice(rows)
        report = x.run_report("2034-01-12")
        self.assertEqual(report["findings"][0]["amount_range"], ["0.00", "22.00"])
        self.assertEqual(report["findings"][0]["verdict"], "conflict")
        self.assertEqual(report["totals"]["total_savings"], "0.00")


class RecordedConflictTests(unittest.TestCase):
    def test_original_live_extractions_keep_the_conflict_despite_delivery_and_identity_unknowns(self):
        root = Path(__file__).resolve().parents[1]
        mailbox = root / "data/examples/conflict/docs"
        fixture = json.loads((root / "tests/fixtures/live_conflict_extracts.json").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            for batch in sorted(mailbox.glob("batch_*")):
                h.ingest(str(batch), str(state))
            (state / "extractions").mkdir()
            for key, value in fixture["extractions"].items():
                (state / "extractions" / f"{key}.json").write_text(json.dumps(value))
            documents = {doc["doc"]: doc for path in state.glob("*/documents.json") for doc in json.loads(path.read_text())}
            agreement = "batch_01/0002.eml#MSA-TBX-US-EXP-E90.pdf"
            amendment = "batch_01/0003.eml#TBX-US-EXP-E90-AMD-1.pdf"
            notice = "batch_02/0001.eml"
            calls = []
            def api(**kwargs):
                def create(**arguments):
                    request = json.loads(arguments["messages"][0]["content"])
                    task = request.get("task", "contract_pricing" if "requested_lane" in request else None)
                    doc = request["document"]["doc"]
                    calls.append((task, doc))
                    ref = {"doc": doc, "page": 1, "quote": documents[doc]["pages"][0]}
                    if task == "pricing_precedence":
                        value = {"notice_vs_amendment": "unranked", "evidence": [{"doc": agreement, "page": 4,
                            "quote": "13.3 Unranked instruments. Where a rate notice under clause 3.5 and an amendment state different rates for the same lane and period, the parties shall resolve the difference jointly; neither prevails automatically."}]}
                    elif task == "billing_policy" and doc == notice:
                        value = {"contract_id": None, "carrier_name": None, "definitions": [], "rules": [],
                            "fx_annex_name": None, "fx_rows": [], "evidence": [ref],
                            "payment": {"due_days": None, "monthly_rate": None, "grace_days": None, "compounds": None,
                                        "exclude_disputed": None, "extensions_permitted": None, "evidence": []}}
                    else:
                        raise AssertionError(f"Existing valid cache unexpectedly missed: {task}, {doc}")
                    if request.get("requested_fields"):
                        value = {key: value[key] for key in request["requested_fields"]}
                    return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(value))])
                return SimpleNamespace(messages=SimpleNamespace(create=create))
            with patch.object(h, "CLAUDE_MODEL", fixture["model"]), patch.object(h, "CLAUDE_EFFORT", fixture["effort"]):
                with patch.object(h, "Anthropic", api):
                    report = h.report("2032-11-30", "supervised", str(state))
                item = report["findings"][0]
                self.assertEqual((item["invoice_id"], item["verdict"], item["action"], item["amount_range"]),
                                 ("TX-US-2032-5166", "conflict", "escalate", ["0.00", "20.87"]))
                self.assertEqual(item["conflict_scope"]["unpriced_line_indices"], [4])
                self.assertTrue({agreement, amendment, notice}.issubset({ref["doc"] for ref in item["evidence"]}))
                self.assertTrue(any(ref["doc"] == agreement and "3.2 Pricing date." in ref["quote"]
                                    for ref in item["evidence"]))
                for key, value in fixture["extractions"].items():
                    self.assertEqual(json.loads((state / "extractions" / f"{key}.json").read_text()), value)
                self.assertEqual(report["totals"]["total_savings"], "0.00")
                self.assertEqual(schema_errors(report), [])
                self.assertEqual(citation_errors(report, mailbox), [])
                self.assertEqual({task for task, doc in calls}, {"pricing_precedence", "billing_policy"})
                with patch.object(h, "Anthropic", side_effect=AssertionError("Replay must not call the API")):
                    self.assertEqual(h.report("2032-11-30", "supervised", str(state)), report)
                    autonomous = h.report("2032-11-30", "autonomous", str(state))
                    self.assertEqual(autonomous["findings"][0]["action"], "escalate")


class RecordedAuthorityTests(unittest.TestCase):
    def test_live_precedence_results_resolve_global_fuel_dates_then_pass_the_cli_with_api_blocked(self):
        from live_mailboxes import build_authority
        from live_checks import check_report, run_case

        root = Path(__file__).resolve().parents[1]
        fixture = json.loads((root / "tests/fixtures/live_authority_extracts.json").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            mailbox, state = directory / "mailbox", directory / "state"
            as_of, expected, probes, _ = build_authority("authority_precedence", mailbox)
            for batch in sorted(mailbox.glob("batch_*")):
                h.ingest(str(batch), str(state))
            (state / "extractions").mkdir()
            for key, value in fixture["extractions"].items():
                (state / "extractions" / f"{key}.json").write_text(json.dumps(value))
            agreement = "batch_01/0001.eml#C-71.pdf"
            calls = []
            def api(**kwargs):
                def create(**arguments):
                    request = json.loads(arguments["messages"][0]["content"])
                    calls.append((request["task"], request["document"]["doc"]))
                    self.assertEqual(calls[-1], ("fuel_date_basis", agreement))
                    value = {"date_basis": "pickup_date", "evidence": [{"doc": agreement, "page": 1,
                        "quote": "All pricing is in force on the pickup date."}]}
                    return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(value))])
                return SimpleNamespace(messages=SimpleNamespace(create=create))
            with patch.object(h, "CLAUDE_MODEL", fixture["model"]), patch.object(h, "CLAUDE_EFFORT", fixture["effort"]):
                with patch.object(h, "Anthropic", api):
                    report = h.report(as_of, "supervised", str(state))
                check_report(report, expected, mailbox)
                self.assertEqual(report["diagnostics"], [])
                self.assertEqual(report["totals"]["total_overpayment"], "33.00")
                self.assertEqual(calls, [("fuel_date_basis", agreement)])
                for key, value in fixture["extractions"].items():
                    self.assertEqual(json.loads((state / "extractions" / f"{key}.json").read_text()), value)
                result = run_case("authority_precedence", mailbox, as_of, expected, probes,
                                  directory / "checks", state_override=state, cached_only=True)
                self.assertEqual((result["status"], result["new_cache_files"]), ("PASS", 0))


if __name__ == "__main__":
    unittest.main()

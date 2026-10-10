"""Glossary action boundaries, scope isolation and real cached pipeline checks."""
import copy
import unittest
from unittest.mock import patch

from harness import actions, harness as h
import test_billing as fixtures


def item(invoice="I-1", amount="10.00", **updates):
    value = {"id": invoice, "class": "overcharge", "invoice_id": invoice, "contract_id": "C-1",
             "verdict": "finding", "amount_usd": amount, "action": "draft:dispute", "confidence": .2,
             "evidence": [{"doc": "held", "page": 1, "quote": "Original deciding source."}],
             "_execution": {"remedy": "dispute", "evidence_complete": True, "source_authority": True,
                            "recipient": {"email": "billing@carrier.example", "person": "Lee", "category": "billing_disputes"},
                            "scope": [{"contract_id": "C-1", "lane": "A-B", "charge": "FRT", "period": "2034-01"}]}}
    value.update(updates)
    return value


def report(items, mode="autonomous"):
    return {"mode": mode, "findings": items, "portfolio": {"cashback": [], "expiry": [], "late_fee_risk": []}}


class ActionPolicyTests(unittest.TestCase):
    def test_all_checks_execute_without_a_confidence_permission_threshold(self):
        finding = item()
        actions.apply(report([finding]))
        self.assertEqual(finding["action"], "execute:dispute")
        self.assertEqual(finding["action_basis"]["failed_checks"], [])
        self.assertNotIn("_execution", finding)
        self.assertEqual(finding["outbound"]["recipient"]["email"], "billing@carrier.example")

    def test_supervised_always_drafts_small_outbound_remedies(self):
        for remedy in actions.REVERSIBLE:
            finding = item()
            finding["_execution"]["remedy"] = remedy
            if remedy == "request_document":
                finding.update(verdict="cannot_determine", amount_usd=None,
                               missing_doc={"name": "Missing Annex", "holder": {"person": "Lee"}})
            actions.apply(report([finding], "supervised"))
            self.assertEqual(finding["action"], "draft:" + remedy)

    def test_missing_evidence_authority_or_recipient_holds_even_at_high_confidence(self):
        for field in ("evidence_complete", "source_authority", "recipient"):
            finding = item(confidence=.999)
            finding["_execution"][field] = False if field != "recipient" else None
            actions.apply(report([finding]))
            self.assertEqual(finding["action"], "human_review")

    def test_irreversible_requests_can_never_execute(self):
        for remedy in ("pay", "settle", "accept_terms", "renew", "unrecognised"):
            finding = item()
            finding["_execution"]["remedy"] = remedy
            actions.apply(report([finding]))
            self.assertEqual(finding["action"], "human_review")
            self.assertFalse(finding["action_basis"]["checks"]["reversible"])

    def test_exact_threshold_and_split_classes_are_aggregated_in_both_modes(self):
        for mode in ("supervised", "autonomous"):
            findings = [item(amount="2500.00"), item(amount="2500.00", **{"class": "duplicate"})]
            findings.append(item("ANOTHER", "4999.99"))
            actions.apply(report(findings, mode))
            self.assertEqual([entry["action"] for entry in findings[:2]], ["human_review"] * 2)
            self.assertEqual(findings[0]["action_basis"]["aggregate_outbound_usd"], "5000.00")
            self.assertEqual(findings[2]["action"], "draft:dispute" if mode == "supervised" else "execute:dispute")

    def test_cashback_threshold_is_per_full_key_and_internal_credit_stays_internal(self):
        value = report([])
        for period, amount in (("2034-Q1", "5000.00"), ("2034-Q2", "10.00")):
            credit = item(amount=amount)
            credit.pop("invoice_id")
            credit.update(**{"class": "cashback", "period": period, "credit_type": "rebate"})
            credit["_execution"]["remedy"] = "claim_credit"
            value["portfolio"]["cashback"].append(credit)
        tracking = copy.deepcopy(value["portfolio"]["cashback"][1])
        tracking.update(period="2034-Q3", action="internal:track_credit")
        value["portfolio"]["cashback"].append(tracking)
        actions.apply(value)
        self.assertEqual([entry["action"] for entry in value["portfolio"]["cashback"]],
                         ["human_review", "execute:claim_credit", "internal:track_credit"])

    def test_conflict_blocks_only_its_contract_lane_charge_and_period(self):
        for field, different in (("contract_id", "C-2"), ("lane", "A-C"), ("charge", "SEC"), ("period", "2034-02"), (None, None)):
            finding = item()
            if field:
                finding["_execution"]["scope"][0][field] = different
            conflict = item("CONFLICT", None, verdict="conflict", amount_range=["0", "10"],
                            conflict_scope={"lanes": ["A-B"], "codes": ["FRT"], "pickup_dates": ["2034-01-02"]})
            actions.apply(report([finding, conflict]))
            self.assertEqual(conflict["action"], "escalate")
            self.assertEqual(finding["action"], "execute:dispute" if field else "human_review")

    def test_unknown_conflict_scope_cannot_prove_disjointness(self):
        finding = item()
        conflict = item("CONFLICT", None, verdict="conflict", amount_range=["0", "10"])
        actions.apply(report([finding, conflict]))
        self.assertEqual(finding["action"], "human_review")

    def test_supported_document_request_can_execute_without_claiming_unknown_money(self):
        finding = item(amount=None, verdict="cannot_determine",
                       missing_doc={"name": "Annex", "holder": {"person": "Lee"}})
        finding["_execution"]["remedy"] = "request_document"
        actions.apply(report([finding]))
        self.assertEqual(finding["action"], "execute:request_document")
        self.assertIsNone(finding["amount_usd"])
        self.assertTrue(finding["action_basis"]["checks"]["under_threshold"])
        finding = item(amount=None, verdict="cannot_determine",
                       missing_doc={"name": "Annex", "holder": {"person": "Lee"}})
        finding["_execution"].update(remedy="request_document", evidence_complete=False)
        actions.apply(report([finding]))
        self.assertEqual(finding["action"], "human_review")

    def test_missing_document_never_authorizes_a_monetary_claim(self):
        finding = item(missing_doc={"name": "Annex", "holder": {"person": "Lee"}})
        actions.apply(report([finding]))
        self.assertEqual(finding["action"], "human_review")
        self.assertFalse(finding["action_basis"]["checks"]["evidence_complete"])


class ActionPipelineTests(unittest.TestCase):
    def setup_invoice(self, *, pickup_kind="pickup_email", sender="operations@bluejay.example", pickup_date="2034-01-02"):
        fixture = fixtures.BillingPipelineTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.invoice([{"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "25"}])
        if pickup_kind:
            doc = fixture.add(f"Carrier picked up shipment BJ-A1 on {pickup_date}.", pickup_kind, sender=sender)
            fixture.responses["document_identification", doc]["awbs"] = ["BJ-A1"]
            fixture.responses["pickup_record", doc] = {"awb": "BJ-A1", "pickup_date": pickup_date, "evidence": fixture.refs(doc)}
        return fixture

    def test_original_carrier_pickup_and_schedule_recipient_execute_then_replay_without_api(self):
        fixture = self.setup_invoice()
        value = fixture.run_report(mode="autonomous")
        finding = value["findings"][0]
        self.assertEqual(finding["action"], "execute:dispute")
        self.assertEqual(finding["outbound"]["recipient"]["email"], "fiona@bluejay.example")
        with patch.object(h, "Anthropic", side_effect=AssertionError("Replay must not call API")):
            self.assertEqual(h.report("2034-01-20", "autonomous", str(fixture.state)), value)
            self.assertEqual(h.report("2034-01-20", "supervised", str(fixture.state))["findings"][0]["action"], "draft:dispute")

    def test_invoice_awb_and_outsider_pickup_do_not_authorize_unattended_action(self):
        for kind, sender in ((None, "operations@bluejay.example"), ("air_waybill", "operations@bluejay.example"),
                             ("pickup_email", "outsider@another.example")):
            fixture = self.setup_invoice(pickup_kind=kind, sender=sender)
            finding = fixture.run_report(mode="autonomous")["findings"][0]
            self.assertEqual(finding["action"], "human_review")
            self.assertFalse(finding["action_basis"]["checks"]["evidence_complete"])

    def test_invoice_with_several_shipments_supports_its_referenced_late_fee_dispute(self):
        fixture = fixtures.BillingPipelineTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.invoice([{"code": "X-SEC", "awb": awb, "quantity": "10", "quantity_unit": "kg", "billed_amount": "20"}
                         for awb in ("BJ-A1", "BJ-A2")])
        fixture.invoice([{"code": "X-LATE", "awb": None, "reference_invoice_id": "BJ-100",
                          "first_fee_month": 1, "fee_month_count": 1, "billed_amount": "2"}],
                        invoice_id="BJ-200", invoice_date="2034-02-01", received="2034-02-02")
        fixture.record("payments", [fixtures.payment(invoice_id="BJ-100", principal="40", open_amount="0",
                                                    status="paid", paid_date="2034-01-31")], received="2034-02-02")
        for awb in ("BJ-A1", "BJ-A2"):
            doc = fixture.add(f"Carrier picked up {awb} on 2034-01-02.", "pickup_email")
            fixture.responses["document_identification", doc]["awbs"] = [awb]
            fixture.responses["pickup_record", doc] = {"awb": awb, "pickup_date": "2034-01-02", "evidence": fixture.refs(doc)}
        value = fixture.run_report("2034-02-03", mode="autonomous")
        finding = next(row for row in value["findings"] if row["class"] == "late_fee_not_owed")
        self.assertEqual(finding["amount_usd"], "2.00")
        self.assertEqual(finding["action"], "execute:dispute")

    def test_attached_declaration_is_a_record_even_without_a_dedicated_kind(self):
        for status in ("unknown", "quoted_only", "draft"):
            fixture = self.setup_invoice()
            doc = fixture.add("Declaration: BJ-A1 is dry ice only.", "other", attachment="declaration.pdf", status=status)
            fixture.responses["billing_record", doc]["shipments"] = [{
                "awb": "BJ-A1", "arrival_date": None, "release_date": None, "delivery": "unknown",
                "goods": "dry_ice_only", "customs_fault": "unknown", "cost": None, "currency": None,
                "evidence": fixture.refs(doc)}]
            fixture.invoice([{"code": "X-DG", "billed_amount": "100"}], invoice_id="BJ-200")
            if status == "unknown":
                value = fixture.run_report(mode="autonomous")
                finding = next(row for row in value["findings"] if row["invoice_id"] == "BJ-200")
                self.assertEqual(finding["action"], "execute:dispute")
            else:
                # No lower-authority declaration can justify a factual charge decision.
                value = fixture.run_report(mode="autonomous")
                self.assertFalse(any(row["invoice_id"] == "BJ-200" for row in value["findings"]))

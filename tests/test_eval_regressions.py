"""Percentage units and customer records must survive a cached eval replay."""
from __future__ import annotations

import copy
import tempfile
import unittest
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path

from harness import cashback, workflow


class PercentageTests(unittest.TestCase):
    def test_fractional_margin_extraction_uses_printed_percentage_points(self):
        reference = {"quote": "Quarter QS reduces MAR from 20% to 10% after the crossing invoice."}
        terms = {"margin": {"rate_before": "0.20", "rate_after": "0.10", "evidence": [reference]},
                 "rebate": None, "annual": []}
        original = copy.deepcopy(terms)
        corrected = cashback._normalise_percentages(terms)
        self.assertEqual(Decimal(corrected["margin"]["rate_before"]), Decimal(20))
        self.assertEqual(Decimal(corrected["margin"]["rate_after"]), Decimal(10))
        self.assertEqual(terms, original)  # Preserve the paid extraction, including its raw units.

    def test_explicit_small_percentage_is_not_multiplied(self):
        refs = [{"quote": "Margin is 0.20%; another charge is 20%."}]
        self.assertEqual(cashback._percent_points("0.20", refs), "0.20")

    def test_missing_percentage_evidence_does_not_allow_conversion(self):
        self.assertEqual(cashback._percent_points("0.20", [{"quote": "USD 20 per shipment."}]), "0.20")

    def test_rebate_and_annual_ratios_use_their_own_cited_rates(self):
        terms = {"margin": None, "rebate": {"tiers": [
                    {"percentage": "0.05", "evidence": [{"quote": "Volume rebate is 5 percent."}]}]},
                 "annual": [{"percentage": "0.02", "evidence": [{"quote": "Annual credit is 2%."}]}]}
        corrected = cashback._normalise_percentages(terms)
        self.assertEqual(Decimal(corrected["rebate"]["tiers"][0]["percentage"]), Decimal(5))
        self.assertEqual(Decimal(corrected["annual"][0]["percentage"]), Decimal(2))


class CustomerMailboxTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name)
        (self.state / "batch_01").mkdir()
        self.document = {"doc": "batch_01/0001.eml#msa.pdf"}
        self.cover = {"doc": "batch_01/0001.eml"}
        self.available = {self.cover["doc"]: self.cover}
        self.metadata = {self.cover["doc"]: {"content_status": "unknown"}}
        self.terms = {"binding_state": "executed", "contacts": [
            {"email": "fiona@carrier.example", "person": "Fiona", "category": "waivers", "party": "carrier"}]}
        self.write_cover()

    def write_cover(self, sender="fiona@carrier.example", recipients="accounts@customer.example"):
        message = EmailMessage()
        message["From"] = sender
        message["To"] = recipients
        message.set_content("Attached is the executed agreement signed by both parties.")
        (self.state / self.cover["doc"]).write_bytes(message.as_bytes())

    def domains(self):
        return workflow._shipper_mailbox_domains(
            self.document, self.terms, self.available, self.metadata, str(self.state))

    def test_customer_payment_facts_do_not_grant_waiver_authority(self):
        self.assertEqual(self.domains(), ["customer.example"])
        terms = {**self.terms, "_shipper_mailbox_domains": self.domains()}
        ledger = {"doc": "batch_01/0002.eml"}
        message = EmailMessage()
        message["From"] = "payments@customer.example"
        message.set_content("The original payment register is attached.")
        (self.state / ledger["doc"]).write_bytes(message.as_bytes())
        self.assertTrue(workflow.company_sender(ledger, terms, str(self.state)))
        self.assertFalse(workflow.company_sender(ledger, terms, str(self.state), parties=("carrier",)))
        self.assertEqual(workflow.authorized_contacts(ledger, terms, str(self.state), "waivers"), [])
        message.replace_header("From", "payments@outsider.example")
        (self.state / ledger["doc"]).write_bytes(message.as_bytes())
        self.assertFalse(workflow.company_sender(ledger, terms, str(self.state)))

    def test_quoted_draft_or_unsigned_agreement_cannot_establish_customer(self):
        for status in ("quoted_only", "draft"):
            self.metadata[self.cover["doc"]]["content_status"] = status
            self.assertEqual(self.domains(), [])
        self.metadata[self.cover["doc"]]["content_status"] = "original"
        self.terms["binding_state"] = "unsigned"
        self.assertEqual(self.domains(), [])

    def test_outsider_or_ambiguous_envelope_cannot_establish_customer(self):
        self.write_cover(sender="fiona@outside.example")
        self.assertEqual(self.domains(), [])
        self.write_cover(recipients="accounts@customer.example, observer@outside.example")
        self.assertEqual(self.domains(), [])
        self.write_cover(recipients="accounts@carrier.example")
        self.assertEqual(self.domains(), [])

    def test_explicit_shipper_contact_overrides_cover_recipient(self):
        self.terms["contacts"].append({"email": "ap@named-customer.example", "party": "shipper"})
        self.assertEqual(self.domains(), [])


if __name__ == "__main__":
    unittest.main()

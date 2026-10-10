"""Independent regressions for combinations required by the assignment glossary."""
from __future__ import annotations

import unittest
import copy
from email import policy
from email.encoders import encode_base64
from email.parser import BytesParser

import pymupdf
from grader.extract import pages_of

import test_billing as fixtures


class UnseenRuleTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.BillingPipelineTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)

    def test_later_invoice_date_wins_even_when_received_first(self):
        f = self.f
        row = {"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "20"}
        f.invoice([row], invoice_id="Z-LATER", invoice_date="2034-01-06", received="2034-01-10")
        f.invoice([row], invoice_id="A-EARLIER", invoice_date="2034-01-05", received="2034-01-11")
        self.assertEqual(f.amounts(f.run_report(), "duplicate"), {"Z-LATER": "20.00"})

    def test_distinct_house_awbs_are_not_duplicates(self):
        f = self.f
        row = {"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "20"}
        f.invoice([{**row, "awb": "HOUSE-A"}], invoice_id="Z-FIRST")
        f.invoice([{**row, "awb": "HOUSE-B"}], invoice_id="A-SECOND", invoice_date="2034-01-06", received="2034-01-11")
        self.assertEqual(f.amounts(f.run_report(), "duplicate"), {})

    def test_same_interest_month_on_two_fee_invoices_is_only_owed_once(self):
        f = self.f
        row = {"code": "X-LATE", "awb": None, "reference_invoice_id": "PRINCIPAL-1",
               "first_fee_month": 1, "fee_month_count": 1, "quantity_unit": "month", "billed_amount": "2"}
        f.invoice([row], invoice_id="FEE-FIRST", invoice_date="2034-01-12")
        f.invoice([row], invoice_id="FEE-AGAIN", invoice_date="2034-01-15")
        f.record("payments", [fixtures.payment("PRINCIPAL-1", due_date="2034-01-01", status="paid",
                                               paid_date="2034-01-10", open_amount="0")])
        self.assertEqual(f.amounts(f.run_report(), "late_fee_not_owed"), {"FEE-AGAIN": "2.00"})

    def test_different_interest_months_on_separate_invoices_remain_clean(self):
        f = self.f
        row = {"code": "X-LATE", "awb": None, "reference_invoice_id": "PRINCIPAL-1",
               "first_fee_month": 1, "fee_month_count": 1, "quantity_unit": "month", "billed_amount": "2"}
        f.invoice([row], invoice_id="FEE-FIRST", invoice_date="2034-01-12")
        f.invoice([{**row, "first_fee_month": 2}], invoice_id="FEE-SECOND", invoice_date="2034-02-10",
                  received="2034-02-10")
        f.record("payments", [fixtures.payment("PRINCIPAL-1", due_date="2034-01-01", status="paid",
                                               paid_date="2034-02-05", open_amount="0")])
        self.assertEqual(f.amounts(f.run_report("2034-02-15"), "late_fee_not_owed"), {})

    def test_partial_interest_bills_can_complete_one_month_without_a_false_dispute(self):
        f = self.f
        row = {"code": "X-LATE", "awb": None, "reference_invoice_id": "PRINCIPAL-1",
               "first_fee_month": 1, "fee_month_count": 1, "quantity_unit": "month"}
        f.invoice([{**row, "billed_amount": "0.50"}], invoice_id="FEE-FIRST", invoice_date="2034-01-12")
        f.invoice([{**row, "billed_amount": "1.50"}], invoice_id="FEE-SECOND", invoice_date="2034-01-15")
        f.record("payments", [fixtures.payment("PRINCIPAL-1", due_date="2034-01-01", status="paid",
                                               paid_date="2034-01-10", open_amount="0")])
        self.assertEqual(f.amounts(f.run_report(), "late_fee_not_owed"), {})

    def test_split_interest_lines_share_one_entitlement_within_an_invoice(self):
        f = self.f
        row = {"code": "X-LATE", "awb": None, "reference_invoice_id": "PRINCIPAL-1",
               "first_fee_month": 1, "fee_month_count": 1, "quantity_unit": "month"}
        f.invoice([{**row, "billed_amount": "0.50"}, {**row, "billed_amount": "3.50"}],
                  invoice_id="FEE-SPLIT", invoice_date="2034-01-12")
        f.record("payments", [fixtures.payment("PRINCIPAL-1", due_date="2034-01-01", status="paid",
                                               paid_date="2034-01-10", open_amount="0")])
        self.assertEqual(f.amounts(f.run_report(), "late_fee_not_owed"), {"FEE-SPLIT": "2.00"})

    def test_future_final_payment_does_not_erase_prior_partial_payments(self):
        f = self.f
        f.invoice([{"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "20"}])
        f.record("payments", [fixtures.payment(status="paid", paid_date="2034-01-28", principal="20",
            open_amount="0", principal_payments=[{"date": "2034-01-15", "amount": "5"},
                                                 {"date": "2034-01-28", "amount": "15"}])])
        risk = f.run_report()["portfolio"]["late_fee_risk"]
        self.assertEqual(len(risk), 1)
        self.assertEqual(risk[0]["amount_usd"], "0.30")  # (20 - 5 actually paid) * 2%.

    def test_itemized_partial_payments_establish_an_omitted_open_balance(self):
        f = self.f
        f.invoice([{"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "20"}])
        f.record("payments", [fixtures.payment(status="partial", principal="20", open_amount=None,
            principal_payments=[{"date": "2034-01-15", "amount": "5"}])])
        risk = f.run_report()["portfolio"]["late_fee_risk"]
        self.assertEqual(len(risk), 1)
        self.assertEqual(risk[0]["amount_usd"], "0.30")

    def test_explicit_open_balance_is_not_reduced_a_second_time(self):
        f = self.f
        f.invoice([{"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "20"}])
        f.record("payments", [fixtures.payment(status="partial", principal="20", open_amount="15",
            principal_payments=[{"date": "2034-01-15", "amount": "5"}])])
        risk = f.run_report()["portfolio"]["late_fee_risk"]
        self.assertEqual(risk[0]["amount_usd"], "0.30")

    def test_partial_status_alone_does_not_invent_a_remaining_balance(self):
        f = self.f
        f.invoice([{"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "20"}])
        f.record("payments", [fixtures.payment(status="partial", principal="20", open_amount=None)])
        self.assertEqual(f.run_report()["portfolio"]["late_fee_risk"], [])

    def _monthly_terms(self):
        f = self.f
        # Print the deciding clauses in the held PDF as well as in the
        # controlled extraction, so the regression remains source-backed.
        path = f.mailbox / f.contract_doc.partition("#")[0]
        message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
        attachment = next(message.iter_attachments())
        clause = ("Account service X-ACT: USD 10 per calendar month in January 2034.\n"
                  "Account service X-ACT: USD 20 per calendar month from 2034-02-01.\n"
                  "For monthly services, the stated service month selects the applicable rate.")
        with pymupdf.open(stream=attachment.get_payload(decode=True), filetype="pdf") as pdf:
            pdf[0].insert_text((25, 650), clause, fontsize=9)
            raw = pdf.tobytes()
        attachment.set_payload(raw)
        del attachment["Content-Transfer-Encoding"]
        encode_base64(attachment)
        path.write_bytes(message.as_bytes())
        f.responses["source", f.contract_doc]["pages"] = pages_of(raw, "pdf")
        refs = [{"doc": f.contract_doc, "page": 1, "quote": clause}]
        january = next(rule for rule in f.policy["rules"] if rule["code"] == "X-ACT")
        january.update(valid_to="2034-01-31", evidence=refs)
        f.policy["rules"].append({**copy.deepcopy(january), "valid_from": "2034-02-01",
                                  "valid_to": None, "rate": "20"})

    def test_monthly_service_uses_its_service_period_not_later_invoice_date(self):
        f = self.f
        self._monthly_terms()
        f.invoice([{"code": "X-ACT", "awb": None, "service_month": "2034-01",
                    "quantity_unit": "month", "billed_amount": "15"}],
                  invoice_date="2034-02-05", received="2034-02-05")
        self.assertEqual(f.amounts(f.run_report("2034-02-10"), "overcharge"), {"BJ-100": "5.00"})

    def test_monthly_period_rates_leave_both_clean_controls_alone(self):
        f = self.f
        self._monthly_terms()
        for month, amount in (("2034-01", "10"), ("2034-02", "20")):
            f.invoice([{"code": "X-ACT", "awb": None, "service_month": month,
                        "quantity_unit": "month", "billed_amount": amount}],
                      invoice_id="MONTH-" + month, invoice_date="2034-02-05", received="2034-02-05")
        self.assertEqual(f.amounts(f.run_report("2034-02-10"), "overcharge"), {})

    def test_full_pipeline_aggregates_waiver_and_overcharge_at_exact_threshold(self):
        f = self.f
        f.invoice([{"code": "X-SEC", "quantity": "10", "quantity_unit": "kg", "billed_amount": "3020"},
                   {"code": "X-DEL", "billed_amount": "2000"}])
        f.operations(delivery="carrier")
        f.record("waivers", [fixtures.waiver(invoice_ids=["BJ-100"])], sender="fiona@bluejay.example")
        for mode in ("supervised", "autonomous"):
            value = f.run_report(mode=mode)
            self.assertEqual(f.amounts(value, "overcharge"), {"BJ-100": "3000.00"})
            self.assertEqual(f.amounts(value, "waived_but_billed"), {"BJ-100": "2000.00"})
            self.assertEqual([item["action"] for item in value["findings"]], ["human_review", "human_review"])


if __name__ == "__main__":
    unittest.main()

"""Real local OCR checks, confidence ordering, and honest eval scoring."""
from __future__ import annotations

import copy
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf

from grader.extract import Mailbox, pages_of
from harness import common, confidence, ocr
from evals.run_evals import CACHE_GUARD, check, check_report, confidence_labels, mailbox_cache

ROOT = Path(__file__).resolve().parents[1]


def scanned_pdf():
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((40, 50), "INVOICE OCR-600\nCurrency USD\nSecurity screening 10 kg 2.60 26.00", fontsize=16)
        image = page.get_pixmap(matrix=pymupdf.Matrix(2, 2)).tobytes("png")
        with pymupdf.open() as scan:
            page = scan.new_page()
            page.insert_image(page.rect, stream=image)
            scan.new_page().insert_text((40, 50), "Native second page stays unchanged.")
            return scan.tobytes()


class OCRTests(unittest.TestCase):
    def test_real_ocr_preserves_mixed_page_numbers_and_caches(self):
        raw = scanned_pdf()
        native = pages_of(raw, "pdf")
        self.assertFalse(native[0].strip())
        with tempfile.TemporaryDirectory() as state:
            result = ocr.read_pdf(raw, native, state)
            self.assertIn("OCR-600", result["pages"][0])
            self.assertIn("26.00", result["pages"][0])
            self.assertEqual(result["pages"][1], native[1])
            self.assertEqual([p["method"] for p in result["page_sources"]], ["ocr", "native"])
            common.validate_quote(result["pages"], 1, "Security screening 10 kg 2.60 26.00")
            with patch.object(ocr, "_engine", side_effect=AssertionError("OCR repeated")):
                self.assertEqual(ocr.read_pdf(raw, native, state), result)

    def test_native_pdf_never_loads_ocr_or_changes_cache_input(self):
        with patch.object(ocr, "_engine", side_effect=AssertionError("Native text needs no OCR")):
            self.assertEqual(ocr.read_pdf(b"unused", ["Native invoice"], ".state"), {"pages": ["Native invoice"]})

    def test_column_boxes_are_read_left_to_right_by_row(self):
        def word(x, y, text):
            return [[[x, y], [x + 15, y], [x + 15, y + 10], [x, y + 10]], text, .9]
        text, tokens = ocr.arrange_words([word(100, 1, "Amount"), word(0, 0, "Charge"),
                                         word(100, 30, "26.00"), word(0, 30, "SEC")])
        self.assertEqual(text, "Charge Amount\nSEC 26.00")
        self.assertEqual(len(tokens), 4)

    def test_corrupted_ocr_cache_is_rejected(self):
        with self.assertRaises(ValueError):
            ocr._validate_cache({"reader": "obsolete", "sha256": "x"}, "x", 1)

    def test_assignment_scan_charge_rows_are_read_locally(self):
        mailbox = Mailbox(ROOT / "data/scenarios")
        raw, _, _ = mailbox.resolve("batch_02/0016.eml#TXI-35-36-0024.pdf")
        with tempfile.TemporaryDirectory() as state:
            result = ocr.read_pdf(raw, pages_of(raw, "pdf"), state)
        self.assertIn("82021.50", result["pages"][0])
        self.assertIn("14895.10", result["pages"][0])
        self.assertIn("TXI/35-36/0024", result["pages"][0])
        self.assertEqual(result["page_sources"][0]["method"], "ocr")


class ConfidenceTests(unittest.TestCase):
    def setUp(self):
        self.docs = {"contract": {"pages": ["Signed agreement"], "doc": "contract"},
                     "invoice": {"pages": ["SEC 10 kg 2.60 26.00"], "doc": "invoice"}}
        self.meta = {"contract": {"kind": "contract", "content_status": "original"},
                     "invoice": {"kind": "invoice", "content_status": "original"}}
        self.contracts = {"C-1": {"document": self.docs["contract"], "terms": {"binding_state": "executed"}}}
        self.item = {"class": "overcharge", "invoice_id": "I-1", "contract_id": "C-1", "verdict": "finding",
                     "evidence": [{"doc": name, "page": 1, "quote": doc["pages"][0]} for name, doc in self.docs.items()],
                     "_confidence_inputs": {"field_support": 1., "clause_in_force": 1., "source_agreement": 1.}}

    def score(self, item=None, diagnostics=None):
        item = copy.deepcopy(item or self.item)
        confidence.assess(item, self.docs, self.meta, self.contracts, diagnostics or [])
        self.assertNotIn("_confidence_inputs", item)
        return item["confidence"]

    def test_weak_numeric_ocr_lowers_confidence_even_with_good_headers(self):
        native = self.score()
        self.docs["invoice"]["page_sources"] = [{"method": "ocr", "min_score": .6,
            "tokens": [{"text": "SEC", "score": .99}, {"text": "26.00", "score": .6}]}]
        self.assertLess(self.score(), native)

    def test_quoted_authority_and_disagreement_lower_probability(self):
        high = self.score()
        self.meta["contract"]["content_status"] = "quoted_only"
        self.assertLess(self.score(), high)
        self.meta["contract"]["content_status"] = "original"
        item = copy.deepcopy(self.item)
        item["_confidence_inputs"]["source_agreement"] = .25
        self.assertLess(self.score(item), high)
        self.assertLess(self.score(diagnostics=[{"invoice_id": "I-1", "reason": "missing clause"}]), high)

    def test_duplicate_citations_do_not_boost_probability(self):
        item = copy.deepcopy(self.item)
        item["evidence"] *= 10
        self.assertEqual(self.score(item), self.score())

    def test_field_support_checks_values_not_just_substrings(self):
        invoice = {"line_items": [{"source_quote": "SEC 10 kg at 2.50 amount 25.00", "quantity": "10", "rate": "2", "billed_amount": "25.00"}]}
        self.assertEqual(confidence.invoice_field_support(invoice), 2 / 3)

    def test_normalised_dates_support_reordered_month_name_layouts(self):
        for printed in ("2035-08-02", "2 Aug 2035", "02 August 2035", "August 2, 2035", "2-Aug-2035"):
            line = {"awb": "NEW123", "pickup_date": "2035-08-02", "lane": "A-B",
                    "quantity": "10", "rate": "2.00", "billed_amount": "20.00",
                    "source_quote": f"NEW123 A-B {printed} 10 kg 2.00 20.00"}
            with self.subTest(printed=printed):
                self.assertEqual(confidence.invoice_field_support({"line_items": [line]}), 1.)
        line["source_quote"] = "NEW123 A-B 22 Aug 2035 10 kg 2.00 20.00"
        self.assertLess(confidence.invoice_field_support({"line_items": [line]}), 1.)

    def test_brier_auc_and_single_label_limitation(self):
        result = confidence.metrics([(.9, True), (.1, False)])
        self.assertEqual(result["brier"], .01)
        self.assertEqual(result["auroc"], 1.)
        self.assertEqual(confidence.metrics([(.5, True), (.5, False)])["auroc"], .5)
        self.assertIsNone(confidence.metrics([(.9, True)])["auroc"])
        self.assertIsNone(confidence.metrics([])["brier"])
        with self.assertRaises(ValueError):
            confidence.metrics([(float("nan"), True)])


class EvalTests(unittest.TestCase):
    def test_runner_keeps_ingestion_errors_even_when_report_matches(self):
        import yaml
        from evals import run_evals

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cases = root / "cases"
            cases.mkdir()
            case = {"mailbox": str(root), "as_of": "2034-01-10", "expect": {
                "key": {"class": "overcharge", "invoice_id": "CLEAN"}, "verdict": "no_finding"}}
            (cases / "own_error.yaml").write_text(yaml.safe_dump(case))
            report = {"as_of": "2034-01-10", "mode": "supervised", "findings": [],
                      "portfolio": {"cashback": [], "expiry": [], "late_fee_risk": []}}
            with patch.object(run_evals, "run", return_value=(report, ["ingest failed"])), contextlib.redirect_stdout(io.StringIO()):
                result = run_evals.main(["--cases", str(cases), "--out", str(root / "results.json"), "--cache-dir", str(root / "cache")])
            self.assertEqual(result, 1)
            self.assertEqual(json.loads((root / "results.json").read_text())["cases"][0]["diff"], ["ingest failed"])

    def test_guarded_harness_runs_in_fresh_process_with_no_documents(self):
        with tempfile.TemporaryDirectory() as state:
            result = subprocess.run([sys.executable, "-c", CACHE_GUARD, str(ROOT / "harness/harness.py"),
                                     "report", "--as-of", "2034-01-10", "--state-dir", state],
                                    capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["findings"], [])

    def test_cache_groups_do_not_mix_mailboxes(self):
        root = Path("out/state_test/extractions")
        self.assertNotEqual(mailbox_cache(root, ROOT / "evals/mailboxes/payment_risk"),
                            mailbox_cache(root, ROOT / "evals/mailboxes/accessorial_fx"))

    def test_holder_checks_tolerate_punctuation_but_reject_other_people(self):
        missing = {"name": "Annex F", "holder": {"party": "Bluejay Air Ltd", "person": "Fiona Reed"}}
        case = {"expect": {"verdict": "cannot_determine", "missing_doc": missing}}
        item = {"verdict": "cannot_determine", "missing_doc": copy.deepcopy(missing)}
        item["missing_doc"]["holder"]["party"] += "."
        self.assertEqual(check(case, item), [])
        item["missing_doc"]["holder"]["person"] = "Other Person"
        self.assertTrue(check(case, item))

    def test_unknown_money_must_remain_null(self):
        case = {"expect": {"verdict": "cannot_determine", "amount_usd": None}}
        self.assertTrue(check(case, {"verdict": "cannot_determine", "amount_usd": "1.00"}))

    def test_clean_controls_cannot_have_money_or_outbound_actions(self):
        case = {"expect": {"verdict": "no_finding", "amount_usd": None, "action": "none"}}
        self.assertEqual(check(case, None), [])
        self.assertTrue(check(case, {"verdict": "no_finding", "amount_usd": "5.00", "action": "draft:dispute"}))

    def test_own_cases_include_source_mailboxes_and_raster_invoices(self):
        import yaml

        files = list((ROOT / "evals/cases").glob("own_*.yaml"))
        self.assertGreaterEqual(len(files), 10)
        for path in files:
            case = yaml.safe_load(path.read_text())
            self.assertTrue(list((ROOT / case["mailbox"]).glob("batch_*/*.eml")), path)
            self.assertIn(case["expect"], case["checks"])
        path = ROOT / "evals/mailboxes/scanned_overcharge"
        raw, _, _ = Mailbox(path).resolve("batch_01/0003.eml#OCR-600.pdf")
        self.assertFalse(any(page.strip() for page in pages_of(raw, "pdf")))

    def test_confidence_labels_ignore_action_but_detect_wrong_amount(self):
        exp = {"key": {"class": "overcharge", "invoice_id": "I-1"}, "amount_usd": "5.00", "action": "draft:dispute"}
        item = {"class": "overcharge", "invoice_id": "I-1", "verdict": "finding", "amount_usd": "5.00",
                "action": "human_review", "confidence": .9}
        report = {"findings": [item], "portfolio": {"cashback": [], "expiry": [], "late_fee_risk": []}}
        self.assertTrue(confidence_labels({"expect": exp}, report)[0][2])
        item["amount_usd"] = "6.00"
        self.assertFalse(confidence_labels({"expect": exp}, report)[0][2])

    def test_eval_checks_do_not_ignore_invalid_schema(self):
        self.assertTrue(check_report({"expect": {}}, {"findings": []}, ROOT))


if __name__ == "__main__":
    unittest.main()

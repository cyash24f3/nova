"""Regression checks for the opt-in live runner and its independent fixtures."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from grader.extract import pages_of
from harness import harness as h
from live_checks import (CASES, NEW_CASES, SYNTHETIC, FEEDBACK_CASES, AUTHORITY_CASES, CheckFailure, check_confidence, check_revisions,
                         compare_answers, date_probes, golden_totals, load_example_case, load_new_case, read_report, run_case)
from live_mailboxes import build, build_feedback, build_authority
from harness import feedback


def report_for(items):
    return {"findings": copy.deepcopy(items), "portfolio": {"cashback": [], "expiry": [], "late_fee_risk": []},
            "totals": golden_totals(items)}


def overcharge(invoice="I-1", **updates):
    return {"class": "overcharge", "invoice_id": invoice, "verdict": "finding",
            "amount_usd": "5.00", "action": "draft:dispute", "deadline": None, **updates}


class AnswerComparisonTests(unittest.TestCase):
    def test_authority_sources_have_independent_money_and_real_arrival_stages(self):
        for name in AUTHORITY_CASES:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                as_of, expected, probes, stages = build_authority(name, root)
                batches = sorted(root.glob("batch_*"))
                self.assertEqual(len(batches), 3 if name == "report_revisions" else 1)
                for batch in batches:
                    h.ingest(str(batch), str(root / "state"))
                documents = [doc for path in (root / "state").glob("*/documents.json") for doc in json.loads(path.read_text())]
                self.assertTrue(all(any(page.strip() for page in doc["pages"]) for doc in documents))
                self.assertEqual(golden_totals(expected)["total_savings"], "33.00" if name == "authority_precedence" else "0.00")
                if name == "unranked_conflict":
                    self.assertEqual(expected[0]["amount_range"], ["0.00", "11.00"])
                if stages:
                    self.assertEqual([stage["as_of"] for stage in stages], ["2034-01-10", "2034-01-11", "2034-01-12"])
                    self.assertEqual(golden_totals(stages[1]["expected"])["total_savings"], "44.00")
                    self.assertEqual(as_of, stages[-1]["as_of"])
                    self.assertEqual(probes[0], (stages[0]["as_of"], stages[0]["expected"]))

    def test_revision_checker_requires_changed_ids_prior_links_new_evidence_and_retro(self):
        key = {"class": "overcharge", "invoice_id": "I-1"}
        previous = report_for([overcharge(id="old-id", supersedes=[], evidence=[])])
        stage = {"batch": "batch_02", "revisions": [key]}
        current = report_for([overcharge(id="new-id", supersedes=["old-id"], evidence=[{"doc": "batch_02/0001.eml"}])])
        check_revisions(previous, current, stage)
        for field, value in (("id", "old-id"), ("supersedes", []), ("evidence", [{"doc": "batch_01/0001.eml"}])):
            damaged = copy.deepcopy(current)
            damaged["findings"][0][field] = value
            with self.subTest(field=field), self.assertRaises(CheckFailure):
                check_revisions(previous, damaged, stage)
        retro = report_for([overcharge(id="retro-id", retro=True, supersedes=[])])
        check_revisions(None, retro, {"retro": [key]})
        retro["findings"][0]["supersedes"] = ["invented-old-id"]
        with self.assertRaises(CheckFailure):
            check_revisions(None, retro, {"retro": [key]})

    def test_feedback_sources_and_expected_learning_have_valid_inputs(self):
        for name in FEEDBACK_CASES:
            with tempfile.TemporaryDirectory() as tmp:
                mailbox = Path(tmp)
                as_of, expected, probes, plan = build_feedback(name, mailbox)
                feedback.validate(plan["feedback"])
                h.ingest(str(mailbox / "batch_01"), str(mailbox / "state"))
                self.assertEqual(as_of, "2034-02-10")
                self.assertEqual(probes, [("2034-02-09", plan["before"])])
                for key, item_id in plan["learned"]:
                    self.assertTrue(any(all(item.get(field) == value for field, value in key.items()) for item in expected))
                    self.assertIn(item_id, [item["item_id"] for item in plan["feedback"]["items"]])

    def test_new_cases_have_independent_expectations_and_receipt_date_probes(self):
        for name in NEW_CASES:
            mailbox, as_of, expected, probes = load_new_case(name)
            self.assertTrue(mailbox.is_dir())
            self.assertEqual(as_of, "2034-01-10")
            self.assertEqual(probes[0], ("2034-01-04", []))
            self.assertEqual(probes[1], ("2034-01-05", expected))
            target = expected[0]
            self.assertEqual(target["verdict"], "no_finding" if name == "scanned_minimum_clean" else "finding")
            if target["verdict"] == "finding":
                self.assertEqual(target["amount_usd"], "6.00")

    def test_live_confidence_check_rejects_missing_basis_and_handles_clean_cases(self):
        report = report_for([overcharge(id="overcharge:I-1", confidence=.9)])
        with self.assertRaisesRegex(CheckFailure, "Missing confidence basis"):
            check_confidence(report)
        self.assertEqual(check_confidence(report_for([]))["items_scored"], 0)

    def test_clean_controls_can_be_absent_but_cannot_be_positive(self):
        expected = [overcharge(), overcharge("CLEAN", verdict="no_finding", amount_usd=None, action="none")]
        report = report_for([overcharge()])
        self.assertEqual(compare_answers(report, expected), [])
        report["findings"].append(overcharge("CLEAN"))
        self.assertTrue(any("Clean control" in error for error in compare_answers(report, expected)))

    def test_wrong_amount_action_and_deadline_are_detected(self):
        expected = [overcharge(deadline="2034-01-20")]
        report = report_for([overcharge(amount_usd="15.00", action="execute:dispute", deadline=None)])
        errors = compare_answers(report, expected)
        self.assertTrue(any("USD expected" in error for error in errors))
        self.assertTrue(any("action expected" in error for error in errors))
        self.assertTrue(any("deadline expected" in error for error in errors))

    def test_null_deadline_still_requires_no_deadline(self):
        errors = compare_answers(report_for([overcharge(deadline="2034-01-20")]), [overcharge()])
        self.assertTrue(any("deadline expected None" in error for error in errors))

    def test_assignment_cashback_deadline_is_checked_independently(self):
        mailbox, as_of, expected, probes = load_example_case("cashback")
        original = json.loads((mailbox.parent / "expected_finding.json").read_text())
        self.assertIsNone(original["portfolio"]["cashback"][0]["deadline"])
        self.assertEqual(as_of, "2033-11-30")
        rebate = next(item for item in expected if item["verdict"] == "finding")
        self.assertEqual(rebate["deadline"], "2033-08-14")
        self.assertEqual(rebate["amount_usd"], "3278.35")
        report = report_for([])
        report["portfolio"]["cashback"] = [copy.deepcopy(rebate)]
        report["totals"] = golden_totals(expected)
        self.assertEqual(compare_answers(report, expected), [])
        report["portfolio"]["cashback"][0]["deadline"] = "2033-08-15"
        self.assertTrue(any("deadline expected" in error for error in compare_answers(report, expected)))

    def test_cashback_fixture_requires_the_original_due_clause(self):
        with patch("live_checks.Mailbox.check", return_value="quote absent"):
            with self.assertRaisesRegex(CheckFailure, "does not match the contract"):
                load_example_case("cashback")

    def test_cashback_probes_cover_quarter_close_and_claim_deadline(self):
        _, _, expected, probes = load_example_case("cashback")
        self.assertEqual([day for day, _ in probes], ["2033-06-30", "2033-07-01", "2033-08-13", "2033-08-14"])
        self.assertEqual(probes[0][1], [])
        self.assertEqual([items[0]["action"] for _, items in probes[1:]],
                         ["internal:track_credit", "internal:track_credit", "draft:claim_credit"])
        self.assertTrue(all(items[0]["amount_usd"] == "3278.35" for _, items in probes[1:]))
        self.assertEqual(expected[0]["action"], "draft:claim_credit")

    def test_missing_and_unexpected_keys_are_both_detected(self):
        errors = compare_answers(report_for([overcharge("OTHER")]), [overcharge()])
        self.assertTrue(any("Missing expected" in error for error in errors))
        self.assertTrue(any("Unexpected item" in error for error in errors))

    def test_missing_document_requires_name_and_holder(self):
        expected = [overcharge(verdict="cannot_determine", amount_usd=None, action="draft:request_document",
                    missing_doc={"name": "Annex F", "holder": {"party": "Bluejay Air Ltd", "person": "Fiona Reed", "role": "treasury"}})]
        report = report_for(expected)
        report["findings"][0]["missing_doc"]["holder"]["party"] += "."
        self.assertEqual(compare_answers(report, expected), [])
        report["findings"][0]["missing_doc"]["holder"]["person"] = "Another Person"
        report["findings"][0]["missing_doc"]["name"] = "Agreement"
        errors = compare_answers(report, expected)
        self.assertTrue(any("missing document name" in error for error in errors))
        self.assertTrue(any("document holder person" in error for error in errors))

    def test_current_autonomous_holds_are_explicit(self):
        expected = [overcharge()]
        report = report_for([overcharge(action="human_review")])
        self.assertEqual(compare_answers(report, expected, mode="autonomous"), [])
        self.assertTrue(compare_answers(report, expected, mode="supervised"))

    def test_autonomous_execution_requires_all_checks_and_a_cited_recipient(self):
        expected = [overcharge()]
        finding = overcharge(action="execute:dispute",
                             action_basis={"checks": dict.fromkeys(("evidence_complete", "no_conflict", "under_threshold",
                                                                    "reversible", "enough_authority"), True)},
                             outbound={"recipient": {"email": "billing@carrier.example"}},
                             evidence=[{"doc": "agreement", "page": 1, "quote": "Schedule 1 billing@carrier.example"}])
        report = report_for([finding])
        self.assertEqual(compare_answers(report, expected, mode="autonomous"), [])
        finding = report["findings"][0]
        finding["action_basis"]["checks"]["no_conflict"] = False
        self.assertTrue(compare_answers(report, expected, mode="autonomous"))
        finding["action_basis"]["checks"]["no_conflict"] = True
        finding["outbound"]["recipient"]["email"] = "invented@another.example"
        self.assertTrue(compare_answers(report, expected, mode="autonomous"))

    def test_portfolio_totals_exclude_expiry_and_unknown_money(self):
        items = [overcharge(), overcharge("UNKNOWN", verdict="cannot_determine", amount_usd=None),
                 {"class": "expiry", "contract_id": "C-1", "verdict": "finding", "amount_usd": None},
                 {"class": "late_fee_risk", "invoice_id": "I-1", "verdict": "finding", "amount_usd": "2.00"}]
        self.assertEqual(golden_totals(items), {"total_overpayment": "5.00", "cashback_owed": "0.00",
                                              "late_fees_avoided": "2.00", "total_savings": "7.00"})

    def test_expiry_probes_include_inclusive_window_edges(self):
        item = {"class": "expiry", "deadline": "2034-03-20"}
        probes = date_probes("expiry", "2034-02-28", [item])
        self.assertEqual(probes, [("2034-01-18", []), ("2034-01-19", [item]),
                                  ("2034-03-20", [item]), ("2034-03-21", [])])


class SourceAndProcessTests(unittest.TestCase):
    def test_fresh_api_verification_refuses_existing_extractions_without_deleting_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mailbox, state = root / "mailbox", root / "state"
            as_of, expected, probes = build("payment_risk", mailbox)
            cache = state / "extractions/existing.json"
            cache.parent.mkdir(parents=True)
            cache.write_text('{"keep": true}')
            with self.assertRaisesRegex(CheckFailure, "empty extraction cache"):
                run_case("payment_risk", mailbox, as_of, expected, probes, root / "out",
                         state_override=state, require_fresh_api=True)
            self.assertEqual(cache.read_text(), '{"keep": true}')

    def test_fresh_api_flag_rejects_cache_only_and_existing_state_overrides(self):
        script = str(Path(__file__).with_name("live_checks.py"))
        for conflict in (["--cached-only"], ["--state-dir", ".state"]):
            result = subprocess.run([sys.executable, script, "--case", "scanned_overcharge", "--fresh-api", *conflict],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 2)
            self.assertIn("--fresh-api cannot be combined", result.stderr)

    def test_every_synthetic_mailbox_is_readable_by_actual_ingest(self):
        for name in SYNTHETIC:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                mailbox, state = root / "mailbox", root / "state"
                as_of, expected, probes = build(name, mailbox)
                for batch in sorted(mailbox.glob("batch_*")):
                    h.ingest(str(batch), str(state))
                docs = [doc for path in state.glob("*/documents.json") for doc in json.loads(path.read_text())]
                self.assertGreater(len(docs), 2)
                self.assertTrue(all(doc["pages"] and any(page.strip() for page in doc["pages"]) for doc in docs))
                self.assertTrue(all(doc["received_at"][:10] <= as_of for doc in docs))
                contract = next(doc for doc in docs if doc["doc"].endswith("#C-71.pdf"))
                self.assertIn("Signed for Shipper: Ken Shah", "\n".join(contract["pages"]))
                self.assertTrue(expected)
                # Every citation can later be checked from the copied originals in the state.
                for doc in docs:
                    if "#" in doc["doc"]:
                        from email import policy
                        from email.parser import BytesParser
                        raw_path, filename = doc["doc"].split("#", 1)
                        mail = BytesParser(policy=policy.default).parsebytes((state / raw_path).read_bytes())
                        attachment = next(part for part in mail.iter_attachments() if part.get_filename() == filename)
                        kind = Path(filename).suffix.lstrip(".")
                        self.assertEqual(pages_of(attachment.get_payload(decode=True), kind), doc["pages"])

    def test_api_guard_is_real_and_does_not_require_a_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            box, state = root / "source", root / "state"
            build("payment_risk", box)
            h.ingest(str(box / "batch_01"), str(state))
            early = read_report(state, "2033-12-31", "supervised", cwd=root, timeout=60, block_api=True)
            self.assertEqual(early["findings"], [])
            with self.assertRaisesRegex(CheckFailure, "Cache replay attempted an API call"):
                read_report(state, "2034-01-10", "supervised", cwd=root, timeout=60, block_api=True)

    def test_repeat_ingestion_checks_all_batches_not_only_last(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mailbox, state = root / "source", root / "state"
            for batch in ("batch_01", "batch_02"):
                (mailbox / batch).mkdir(parents=True)
                (state / batch).mkdir(parents=True)
                (state / batch / "documents.json").write_text("[]")
            calls = []

            def changed_first_batch(arguments, **kwargs):
                calls.append(arguments)
                if len(calls) == 3:
                    (state / "batch_01/documents.json").write_text('[{"changed": true}]')
                return ""

            with patch("live_checks.run_cli", side_effect=changed_first_batch):
                with self.assertRaisesRegex(CheckFailure, "Re-ingesting"):
                    run_case("duplicate", mailbox, "2034-01-10", [], [], root / "out", state_override=state)

    def test_api_guard_failure_never_creates_an_empty_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            box = root / "source"
            as_of, expected, probes = build("payment_risk", box)
            with self.assertRaises(CheckFailure):
                run_case("payment_risk", box, as_of, expected, probes, root / "out", cached_only=True)
            self.assertFalse((root / "out/report.json").exists())

    def test_runner_is_opt_in_and_lists_every_case(self):
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("live_checks.py")), "--list"],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in CASES:
            self.assertIn(name + ":", result.stdout)
        self.assertIn("feedback", result.stdout)


if __name__ == "__main__":
    unittest.main()

"""Bounded API concurrency, shared cache misses and deterministic pricing."""
import json
import shutil
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness import billing, common, harness, workflow
import test_billing as fixtures


class CacheConcurrencyTests(unittest.TestCase):
    def test_same_key_extracts_and_repairs_once_with_independent_results(self):
        with tempfile.TemporaryDirectory() as state:
            start = threading.Barrier(5)
            extracting, release = threading.Event(), threading.Event()
            calls = []

            def extract():
                calls.append("extract")
                extracting.set()
                if not release.wait(3):
                    raise AssertionError("Extraction was not released")
                return {"amount": "bad"}

            def validate(value):
                if value["amount"] == "bad":
                    raise ValueError("Rejected amount")

            def repair(value, error):
                calls.append("repair")
                return {"amount": "12.00"}

            def read():
                start.wait(3)
                return common.cached_json(state, "shared", extract, validate, repair)

            with ThreadPoolExecutor(max_workers=5) as pool:
                jobs = [pool.submit(read) for _ in range(5)]
                try:
                    self.assertTrue(extracting.wait(3))
                    self.assertFalse((Path(state) / "extractions/shared.json").exists())
                finally:
                    release.set()
                results = [job.result() for job in jobs]
            self.assertEqual(calls, ["extract", "repair"])
            self.assertEqual(results, [{"amount": "12.00"}] * 5)
            results[0]["amount"] = "mutated"
            self.assertEqual(results[1]["amount"], "12.00")
            self.assertEqual(json.loads((Path(state) / "extractions/shared.json").read_text()),
                             {"amount": "12.00"})

    def test_different_keys_can_extract_at_the_same_time(self):
        with tempfile.TemporaryDirectory() as state:
            simultaneous = threading.Barrier(2)

            def read(key):
                def extract():
                    simultaneous.wait(3)
                    return {"key": key}
                return common.cached_json(state, key, extract, lambda value: None)

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(read, ["first", "second"]))
            self.assertEqual(results, [{"key": "first"}, {"key": "second"}])

    def test_failed_extraction_releases_lock_without_creating_cache(self):
        with tempfile.TemporaryDirectory() as state:
            def fail():
                raise RuntimeError("Provider failed")
            with self.assertRaisesRegex(RuntimeError, "Provider failed"):
                common.cached_json(state, "retry", fail, lambda value: None)
            self.assertFalse((Path(state) / "extractions/retry.json").exists())
            self.assertEqual(common.cached_json(state, "retry", lambda: {"ok": True},
                                                lambda value: None), {"ok": True})


class RequestConcurrencyTests(unittest.TestCase):
    def test_nested_schema_requests_share_one_api_limit(self):
        saturated, release = threading.Event(), threading.Event()
        guard = threading.Lock()
        active = peak = 0
        calls = []

        def create(**arguments):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
                calls.append(arguments)
                if active == common.API_WORKERS:
                    saturated.set()
            try:
                if not release.wait(5):
                    raise AssertionError("API requests were not released")
                fields = arguments["output_config"]["format"]["schema"]["properties"]
                value = {field: field for field in fields}
                return SimpleNamespace(stop_reason="end_turn", content=[
                    SimpleNamespace(type="text", text=json.dumps(value))])
            finally:
                with guard:
                    active -= 1

        backend = SimpleNamespace(CLAUDE_MODEL="test-model", CLAUDE_EFFORT="high",
                                  Anthropic=lambda **kwargs: SimpleNamespace(
                                      messages=SimpleNamespace(create=create)))
        schema = workflow._object({field: {"type": "string"} for field in ("a", "b", "c", "d")})
        groups = [(field,) for field in schema["properties"]]

        def extract(index):
            return common.request_json(backend, {"index": index}, "Read original sources.", schema,
                                       task="test", max_tokens=100, field_groups=groups)

        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = [pool.submit(extract, index) for index in range(4)]
            try:
                self.assertTrue(saturated.wait(5), "Independent parts should run concurrently")
                self.assertEqual(active, common.API_WORKERS)
            finally:
                release.set()
            results = [job.result() for job in jobs]
        self.assertEqual(peak, common.API_WORKERS)
        self.assertEqual(len(calls), 16)
        self.assertEqual(results, [{field: field for field in schema["properties"]}] * 4)
        self.assertTrue(all(list(result) == list(schema["properties"]) for result in results))
        self.assertTrue(all(call["model"] == "test-model" and
                            call["output_config"]["effort"] == "high" for call in calls))


class PricingConcurrencyTests(unittest.TestCase):
    def invoice_fixture(self):
        fixture = fixtures.BillingPipelineTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        for invoice_id, awb in (("BJ-100", "BJ-A1"), ("BJ-200", "BJ-A2")):
            fixture.invoice([{"code": "X-SEC", "quantity": "10", "quantity_unit": "kg",
                              "billed_amount": "25", "awb": awb},
                             {"code": "X-DG", "billed_amount": "100", "awb": awb}],
                            invoice_id=invoice_id)
        harness.ingest(str(fixture.batch), str(fixture.state))
        return fixture

    def test_reverse_completion_keeps_report_diagnostics_and_replay_identical(self):
        fixture = self.invoice_fixture()
        parallel_state = fixture.root / "parallel"
        shutil.copytree(fixture.state, parallel_state)
        with patch.object(harness, "Anthropic", fixture.api), \
                patch.object(billing, "API_WORKERS", 1), patch.object(common, "API_WORKERS", 1):
            serial = harness.report("2034-01-20", "supervised", str(fixture.state))

        simultaneous = threading.Barrier(2)
        second_finished = threading.Event()
        original_price = billing._price_with_conflicts

        def price(record, context, policies):
            simultaneous.wait(3)
            if record["invoice"]["invoice_id"] == "BJ-100":
                if not second_finished.wait(3):
                    raise AssertionError("The other invoice did not finish")
            original_price(record, context, policies)
            if record["invoice"]["invoice_id"] == "BJ-200":
                second_finished.set()

        with patch.object(harness, "Anthropic", fixture.api), \
                patch.object(billing, "_price_with_conflicts", price):
            parallel = harness.report("2034-01-20", "supervised", str(parallel_state))
        self.assertTrue(parallel["diagnostics"])
        self.assertEqual(parallel, serial)
        with patch.object(harness, "Anthropic", side_effect=AssertionError("Replay used the API")):
            self.assertEqual(harness.report("2034-01-20", "supervised", str(parallel_state)), parallel)

    def test_ready_invoice_prices_while_another_is_still_being_annotated(self):
        fixture = self.invoice_fixture()
        second_priced = threading.Event()
        original_prepare = billing._prepare_invoice
        original_price = billing._price_with_conflicts

        def prepare(invoice, *arguments):
            if invoice["invoice_id"] == "BJ-100" and not second_priced.wait(3):
                raise AssertionError("A ready invoice was blocked by another invoice's annotations")
            return original_prepare(invoice, *arguments)

        def price(record, context, policies):
            original_price(record, context, policies)
            if record["invoice"]["invoice_id"] == "BJ-200":
                second_priced.set()

        with patch.object(harness, "Anthropic", fixture.api), \
                patch.object(billing, "_prepare_invoice", prepare), \
                patch.object(billing, "_price_with_conflicts", price):
            report = harness.report("2034-01-20", "supervised", str(fixture.state))
        self.assertTrue(second_priced.is_set())
        self.assertEqual({entry["invoice_id"] for entry in report["diagnostics"]}, {"BJ-100", "BJ-200"})

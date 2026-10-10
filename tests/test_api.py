"""Exercise the real Anthropic SDK against a local schema-limited HTTP server."""
import json
import copy
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from harness import authority, billing as b, cashback as c, common, harness as h, workflow as w


class APIProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name)
        self.document = {"doc": "batch/agreement.pdf", "pages": [
            "Agreement C-71 with Green Parcel Ltd. Invoice I-71 is mentioned."]}
        self.policy = {"contract_id": "C-71", "carrier_name": "Green Parcel Ltd",
            "definitions": [], "rules": [], "fx_annex_name": None, "fx_rows": [],
            "payment": {"due_days": None, "monthly_rate": None, "grace_days": None,
                        "compounds": None, "exclude_disputed": None,
                        "extensions_permitted": None, "evidence": []}, "evidence": []}
        self.record = {"shipments": [], "payments": [], "credits": [], "waivers": [],
                       "extensions": [], "mail_intent": "other", "mentioned_invoice_ids": ["I-71"],
                       "evidence": []}
        self.terms = {"contract_id": "C-71", "binding_state": "unknown", "changes_term": False,
                      "effective_date": None, "expiry_date": None, "auto_renews": None,
                      "renewal_months": None, "notice_days": None, "notice_deadline": None,
                      "extension_policy": "unknown", "notice_sent_date": None,
                      "notice_for_expiry_date": None, "contacts": [], "evidence": []}
        self.calls = []
        self.reject_payment = False
        self.stop_reasons = []
        self.invalid_policy = None
        self.cashback = None
        self.lines = None
        self.contract = None
        self.invalid_contract = None
        self.reject_annual = False
        self.precedence = None
        self.fuel_date = None
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                test.calls.append(request)
                schema = request["output_config"]["format"]["schema"]
                body = json.loads(request["messages"][0]["content"])
                task = body.get("task", "contract_pricing" if "requested_lane" in body else "invoice")

                def count_type_arrays(value):
                    if isinstance(value, dict):
                        return int(isinstance(value.get("type"), list)) + sum(count_type_arrays(x) for x in value.values())
                    if isinstance(value, list):
                        return sum(count_type_arrays(x) for x in value)
                    return 0

                unions = count_type_arrays(schema)
                large_cashback = task == "cashback_terms" and (
                    len(set(schema["properties"]) & {"rebate", "margin", "annual"}) > 1
                    or test.reject_annual and "annual" in schema["properties"])
                rejected = (unions > 16 or task == "reject"
                            or large_cashback or (test.reject_payment and "payment" in schema["properties"]))
                if rejected:
                    response = {"type": "error", "error": {"type": "invalid_request_error",
                        "message": ("The compiled grammar is too large, which would cause performance issues."
                                    if large_cashback else
                                    "Schema compilation rejected: union parameter limit is 16. test-schema-key")}}
                    status = 400
                else:
                    value = {"billing_policy": test.policy, "contract_terms": test.terms,
                             "cashback_terms": test.cashback, "billing_lines": test.lines,
                             "contract_pricing": test.contract, "pricing_precedence": test.precedence,
                             "fuel_date_basis": test.fuel_date}.get(task, test.record)
                    if task == "billing_policy" and test.invalid_policy is not None and "previous_result" not in body:
                        value = test.invalid_policy
                    if task == "contract_pricing" and test.invalid_contract is not None and "previous_result" not in body:
                        value = test.invalid_contract
                    partial = {field: value[field] for field in schema["properties"]}
                    stop = test.stop_reasons.pop(0) if test.stop_reasons else "end_turn"
                    response = {"id": "msg_schema_test", "type": "message", "role": "assistant",
                        "content": [{"type": "text", "text": "{\"unfinished\":" if stop == "max_tokens" else json.dumps(partial)}],
                        "model": h.CLAUDE_MODEL,
                        "stop_reason": stop, "stop_sequence": None,
                        "usage": {"input_tokens": 1, "output_tokens": 1}}
                    status = 200
                data = json.dumps(response).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("request-id", "req-schema-test")
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.environment = patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "test-schema-key",
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{self.server.server_port}",
            "NO_PROXY": "127.0.0.1,localhost"})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def test_precedence_uses_the_real_sdk_structured_schema_and_replays_without_api(self):
        document = {"doc": "batch/order.pdf", "pages": [
            "Agreement C-71. An authorised rate notice and an amendment are unranked; neither prevails automatically."]}
        self.precedence = {"notice_vs_amendment": "unranked", "evidence": [
            {"doc": document["doc"], "page": 1, "quote": document["pages"][0]}]}
        context = {"backend": h, "state_dir": str(self.state)}
        self.assertEqual(authority.precedence(document, context), self.precedence)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["output_config"]["format"]["schema"], authority.PRECEDENCE_SCHEMA)
        with patch.object(h, "Anthropic", side_effect=AssertionError("Cache replay called API")):
            self.assertEqual(authority.precedence(document, context), self.precedence)

    def test_explicit_freight_and_fuel_keep_their_category_without_a_catalog_code(self):
        for category, label in (("base_freight", "Air freight"), ("fuel", "Fuel surcharge")):
            with self.subTest(category=category):
                quote = f"Invoice I-71: {label} for shipment GP-A1."
                document = {"doc": f"batch/{category}.pdf", "pages": [quote]}
                invoice = {"line_items": [{"description": label, "awb": "GP-A1"}]}
                self.lines = {"lines": [{"line_index": 1, "code": None, "category": category,
                    "shipment_id": "GP-A1", "service_month": None, "service_key": None,
                    "reference_invoice_id": None, "first_fee_month": None, "fee_month_count": None,
                    "evidence": [{"doc": document["doc"], "page": 1, "quote": quote}]}]}
                expected = {1: self.lines["lines"][0]}
                self.assertEqual(b.annotate_invoice(invoice, document, [], str(self.state), h), expected)
                with patch.object(h, "Anthropic", side_effect=AssertionError("Cached annotation must replay")):
                    self.assertEqual(b.annotate_invoice(invoice, document, [], str(self.state), h), expected)
        self.assertEqual(len(self.calls), 2)

    def test_fuel_date_resolution_uses_the_scoped_clause_and_replays_without_overwriting_pricing_facts(self):
        cases = (
            ("All pricing, including fuel, uses the pickup date.", "pickup_date"),
            ("All pricing uses pickup dates except fuel, whose rate uses the invoice date.", "invoice_date"),
            ("Base freight uses pickup dates. Fuel has a monthly rate but no selecting date is stated.", "unknown"),
        )
        for clause, basis in cases:
            with self.subTest(basis=basis):
                self.freight_facts()
                fuel_clause = "Fuel is 10 percent of correct base freight only; January 2034 rate is 10 percent."
                self.document["pages"][0] += "\n" + fuel_clause + "\n" + clause
                legacy = [{"source_page": 1, "source_quote": fuel_clause}]
                self.contract["fuel_terms"] = {"date_basis": "unknown", "charge_base": "base_freight_only",
                    "monthly_rates": [{"month": "2034-01", "percentage": "10", "evidence": legacy}], "evidence": legacy}
                self.fuel_date = {"date_basis": basis, "evidence": [] if basis == "unknown" else [
                    {"doc": self.document["doc"], "page": 1, "quote": clause}]}
                agreement = {"document": self.document, "terms": {"contract_id": "C-71", "expiry_date": "2034-12-31"}}
                context = {"backend": h, "state_dir": str(self.state), "metadata": {
                    self.document["doc"]: {"effective_date": "2034-01-01"}}}
                resolved = authority.contract_prices(self.document, agreement, "WEST-EAST", context)
                self.assertEqual(resolved["fuel_terms"]["date_basis"], basis)
                if basis == "pickup_date":
                    self.assertEqual(str(h.calculate_fuel_charge(resolved["fuel_terms"], "2034-01-02", h.Decimal("40"))), "4.00")
                else:
                    with self.assertRaisesRegex(ValueError, "requires pickup-date fuel pricing"):
                        h.calculate_fuel_charge(resolved["fuel_terms"], "2034-01-02", h.Decimal("40"))
                raw = h.cached_extract_contract(self.document, "WEST-EAST", str(self.state))
                self.assertEqual(raw, self.contract)
                with patch.object(h, "Anthropic", side_effect=AssertionError("Fuel date replay called API")):
                    self.assertEqual(authority.contract_prices(self.document, agreement, "WEST-EAST", context), resolved)

    def test_a_resolved_fuel_date_requires_deciding_evidence_before_it_can_be_cached(self):
        self.fuel_date = {"date_basis": "pickup_date", "evidence": []}
        context = {"backend": h, "state_dir": str(self.state)}
        with self.assertRaisesRegex(ValueError, "fuel pricing date needs its deciding contractual clause"):
            authority.fuel_date_basis(self.document, context)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(list((self.state / "extractions").glob("*.json")), [])

    def test_a_known_code_still_requires_its_contractual_category(self):
        quote = "Invoice I-71: FRT Air freight for shipment GP-A1."
        document = {"doc": "batch/invoice.pdf", "pages": [quote]}
        invoice = {"line_items": [{"description": "FRT Air freight", "awb": "GP-A1"}]}
        self.lines = {"lines": [{"line_index": 1, "code": "FRT", "category": "other",
            "shipment_id": "GP-A1", "service_month": None, "service_key": None,
            "reference_invoice_id": None, "first_fee_month": None, "fee_month_count": None,
            "evidence": [{"doc": document["doc"], "page": 1, "quote": quote}]}]}
        definitions = [{"code": "FRT", "category": "base_freight", "labels": ["Air freight"], "evidence": []}]
        with self.assertRaisesRegex(ValueError, "disagrees with the contractual code catalog"):
            b.annotate_invoice(invoice, document, definitions, str(self.state), h)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(list((self.state / "extractions").glob("*.json")), [])

    def freight_facts(self):
        quote = ("Agreement C-71: Air freight USD 4 per kg for all weights on every lane, minimum USD 0. "
                 "Pricing uses pickup dates 2034-01-01 through 2034-12-31.")
        self.document["pages"] = [quote]
        refs = [{"source_page": 1, "source_quote": quote}]
        self.contract = {"contract_id": "C-71", "effective_date": "2034-01-01", "expiry_date": "2034-12-31",
            "pricing_date_basis": "pickup_date", "freight_rates": [{"lane": "WEST-EAST", "currency": "USD",
                "valid_from": "2034-01-01", "valid_to": "2034-12-31", "weight_min_kg": None,
                "weight_max_kg": None, "all_weights": True, "rate_per_kg": "4", "minimum_charge": "0",
                "continues_after_period": False, "evidence": refs}],
            "fuel_terms": {"date_basis": "unknown", "charge_base": "unknown", "monthly_rates": [], "evidence": []},
            "evidence": refs}

    def test_all_weight_fact_prices_unprinted_limits_and_replays_from_cache(self):
        self.freight_facts()
        contract = h.cached_extract_contract(self.document, "WEST-EAST", str(self.state))
        rates = h.find_freight_rates(contract, "WEST-EAST", "USD", "2034-01-02", "10")
        self.assertEqual(len(rates), 1)
        self.assertEqual(str(h.calculate_freight_charge(rates[0], "10")), "40.00")
        self.assertIsNone(contract["freight_rates"][0]["weight_min_kg"])
        with patch.object(h, "Anthropic", side_effect=AssertionError("Cache must preserve the all-weight fact")):
            self.assertEqual(h.cached_extract_contract(self.document, "WEST-EAST", str(self.state)), contract)
        self.assertEqual(len(self.calls), 1)

    def test_unknown_weight_limits_are_not_treated_as_all_weights(self):
        self.freight_facts()
        self.contract["freight_rates"][0]["all_weights"] = False
        with self.assertRaisesRegex(ValueError, "missing weight_min_kg"):
            h.find_freight_rates(self.contract, "WEST-EAST", "USD", "2034-01-02", "10")

    def test_pricing_source_correction_validates_and_preserves_the_original_cache_key(self):
        self.freight_facts()
        self.invalid_contract = copy.deepcopy(self.contract)
        self.invalid_contract["freight_rates"][0]["evidence"][0]["source_quote"] = "An invented quote that is absent from the original page."
        with redirect_stderr(StringIO()):
            result = h.cached_extract_contract(self.document, "WEST-EAST", str(self.state))
        self.assertEqual(result, self.contract)
        self.assertEqual(len(self.calls), 2)
        repair = json.loads(self.calls[1]["messages"][0]["content"])
        self.assertEqual(repair["document"], self.document)
        self.assertEqual(repair["requested_lane"], "WEST-EAST")
        self.assertEqual(repair["previous_result"], self.invalid_contract)
        self.assertIn("page 1", repair["validation_error"])
        self.assertIn("absent", repair["validation_error"])
        self.assertTrue(all(call["max_tokens"] == 16384 for call in self.calls))
        key = h.extraction_cache_key({"document": self.document, "requested_lane": "WEST-EAST"},
                                     h.CONTRACT_PROMPT, h.CONTRACT_SCHEMA)
        self.assertEqual(json.loads((self.state / "extractions" / f"{key}.json").read_text()), self.contract)
        rejected = json.loads((self.state / "rejected" / f"{key}.json").read_text())
        self.assertEqual(rejected["previous_result"], self.invalid_contract)
        with patch.object(h, "Anthropic", side_effect=AssertionError("Validated repair must replay without the API")):
            self.assertEqual(h.cached_extract_contract(self.document, "WEST-EAST", str(self.state)), self.contract)

    def test_pricing_correction_stops_after_one_attempt_and_never_caches_an_invention(self):
        self.freight_facts()
        self.contract["evidence"][0]["source_quote"] = "An invented quote that is absent from the original page."
        with redirect_stderr(StringIO()), self.assertRaisesRegex(ValueError, "after one correction.*page 1.*absent"):
            h.cached_extract_contract(self.document, "WEST-EAST", str(self.state))
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(list((self.state / "extractions").glob("*.json")), [])
        self.assertEqual(len(list((self.state / "rejected").glob("*.json"))), 1)

    def test_pricing_truncation_retries_the_larger_budget_and_caches_only_complete_facts(self):
        self.freight_facts()
        self.stop_reasons = ["max_tokens", "end_turn"]
        with redirect_stderr(StringIO()):
            self.assertEqual(h.cached_extract_contract(self.document, "WEST-EAST", str(self.state)), self.contract)
        self.assertEqual([call["max_tokens"] for call in self.calls], [16384, 32768])
        self.assertEqual(len(list((self.state / "extractions").glob("*.json"))), 1)
        self.assertEqual(list((self.state / "rejected").glob("*.json")), [])

    def test_bounded_weight_rules_keep_their_limits(self):
        self.freight_facts()
        self.contract["freight_rates"][0].update(all_weights=False, weight_min_kg="5", weight_max_kg="15")
        for weight, expected in (("4", 0), ("5", 1), ("15", 1), ("16", 0)):
            self.assertEqual(len(h.find_freight_rates(self.contract, "WEST-EAST", "USD", "2034-01-02", weight)), expected)

    def test_all_weight_fact_cannot_override_an_explicit_weight_limit(self):
        self.freight_facts()
        for lower, upper in (("5", None), (None, "15")):
            contract = copy.deepcopy(self.contract)
            contract["freight_rates"][0].update(weight_min_kg=lower, weight_max_kg=upper)
            with self.assertRaisesRegex(ValueError, "contradicts its weight limits"):
                h.validate_contract(contract, self.document, "WEST-EAST")

    def test_policy_split_preserves_all_fields_nulls_and_uses_one_complete_cache(self):
        self.assertEqual(b.extract_policy(self.document, str(self.state), h), self.policy)
        self.assertGreater(len(self.calls), 1)
        fields = [field for request in self.calls
                  for field in request["output_config"]["format"]["schema"]["properties"]]
        self.assertCountEqual(fields, self.policy)
        caches = list((self.state / "extractions").glob("*.json"))
        self.assertEqual(len(caches), 1)
        self.assertEqual(json.loads(caches[0].read_text()), self.policy)
        count = len(self.calls)
        self.assertEqual(b.extract_policy(self.document, str(self.state), h), self.policy)
        self.assertEqual(len(self.calls), count)
        self.assertEqual(self.calls[0]["model"], h.CLAUDE_MODEL)
        self.assertEqual(self.calls[0]["output_config"]["effort"], "high")

    def test_instrument_policy_can_use_a_verified_printed_parent_without_changing_its_cache(self):
        self.document["pages"] = ["Reference: C-71/RN-3. Revised freight rates; all other terms unchanged."]
        self.policy["contract_id"] = None
        resolved = b.extract_policy(self.document, str(self.state), h, contract_id="C-71")
        self.assertEqual(resolved["contract_id"], "C-71")
        caches = list((self.state / "extractions").glob("*.json"))
        self.assertEqual(len(caches), 1)
        before = caches[0].read_bytes()
        self.assertIsNone(json.loads(before)["contract_id"])
        with patch.object(h, "Anthropic", side_effect=AssertionError("Known instrument policy must replay")):
            self.assertEqual(b.extract_policy(self.document, str(self.state), h, contract_id="C-71"), resolved)
        self.assertEqual(caches[0].read_bytes(), before)

    def test_policy_parent_requires_a_printed_match_and_cannot_replace_an_invented_identity(self):
        self.document["doc"] = "batch/C-7-notice.pdf"
        self.document["pages"] = ["Reference: C-71/RN-3. Revised freight rates; all other terms unchanged."]
        for parent, extracted in (("C-7", None), ("C-71", "MADE-UP-71"), ("C-7", "C-71"), ("C-7", "C-7")):
            with self.subTest(parent=parent, extracted=extracted):
                self.policy["contract_id"] = extracted
                with self.assertRaisesRegex(ValueError, "supported contract identity|different parent contract"):
                    b.extract_policy(self.document, str(self.state), h, contract_id=parent)
                self.assertEqual(list((self.state / "extractions").glob("*.json")), [])

    def test_record_split_preserves_all_fact_groups(self):
        self.assertEqual(b.extract_record(self.document, [], str(self.state), h), self.record)
        self.assertGreater(len(self.calls), 1)
        fields = [field for request in self.calls
                  for field in request["output_config"]["format"]["schema"]["properties"]]
        self.assertCountEqual(fields, self.record)

    def cashback_facts(self):
        self.document["pages"].append(
            "Qualifying Spend is billed SEC, COST and MAR, net of charge credits dated in the period.\n"
            "Rebate and annual improvement credits are excluded. Quarter QS at least USD 100 earns 5% on all QS;\n"
            "credit due 30 days after quarter end. After the crossing invoice, MAR drops from 20% to 10% of COST;\n"
            "margin step-down credit due 30 days after quarter end. Annual improvement is 2% of prior contract-year QS;\n"
            "credit due 60 days after the anniversary.")
        refs = [{"doc": self.document["doc"], "page": 2, "quote": self.document["pages"][1]}]
        self.cashback = {
            "contract_id": "C-71", "spend_categories": ["security", "cost", "margin"], "spend_codes": [],
            "spend_basis": "billed", "net_charge_credits": True,
            "excluded_credit_types": ["rebate", "promised_credit"], "evidence": refs,
            "rebate": {"tiers": [{"threshold_usd": "100", "percentage": "5", "evidence": refs}],
                       "tier_basis": "all_spend", "due_days": 30, "evidence": refs},
            "margin": {"threshold_usd": "100", "rate_before": "20", "rate_after": "10",
                       "base_codes": ["COST"], "margin_codes": ["MAR"],
                       "trigger": "after_crossing_invoice", "due_days": 30, "evidence": refs},
            "annual": [{"percentage": "2", "fixed_usd": None, "minimum_spend_usd": None,
                        "due_days": 60, "evidence": refs}]}
        return [{"code": "COST"}, {"code": "MAR"}]

    def test_combined_cashback_grammar_can_fail_below_union_limit(self):
        definitions = self.cashback_facts()
        self.assertLessEqual(common._union_count(c.TERMS_SCHEMA), 16)
        with self.assertRaisesRegex(RuntimeError, "HTTP 400.*compiled grammar is too large"):
            common.request_json(h, {"task": "cashback_terms", "document": self.document, "definitions": definitions},
                                c.TERMS_PROMPT, c.TERMS_SCHEMA, task="cashback_terms", max_tokens=16384)
        self.assertEqual(len(self.calls), 1)

    def test_cashback_small_requests_merge_validate_and_replay_one_complete_cache(self):
        definitions = self.cashback_facts()
        self.assertEqual(c.extract_terms(self.document, definitions, str(self.state), h), self.cashback)
        self.assertEqual(len(self.calls), 4)
        for call in self.calls:
            schema = call["output_config"]["format"]["schema"]
            self.assertLessEqual(len(set(schema["properties"]) & {"rebate", "margin", "annual"}), 1)
            self.assertLessEqual(common._union_count(schema), 5)
            request = json.loads(call["messages"][0]["content"])
            self.assertEqual(request["document"], self.document)
            self.assertEqual(request["definitions"], definitions)
            self.assertCountEqual(request["requested_fields"], schema["properties"])
        caches = list((self.state / "extractions").glob("*.json"))
        self.assertEqual(len(caches), 1)
        self.assertEqual(json.loads(caches[0].read_text()), self.cashback)
        key = h.extraction_cache_key({"task": "cashback_terms", "document": self.document, "definitions": definitions},
                                     prompt=c.TERMS_PROMPT, schema=c.TERMS_SCHEMA)
        self.assertEqual(caches[0].stem, key)
        with patch.object(h, "Anthropic", side_effect=AssertionError("Replay must not call the API")):
            self.assertEqual(c.extract_terms(self.document, definitions, str(self.state), h), self.cashback)

    def test_failed_last_cashback_part_never_caches_a_partial_extraction(self):
        definitions = self.cashback_facts()
        self.reject_annual = True
        with self.assertRaisesRegex(RuntimeError, "HTTP 400.*compiled grammar is too large"):
            c.extract_terms(self.document, definitions, str(self.state), h)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(list((self.state / "extractions").glob("*.json")), [])

    def test_invalid_field_partitions_are_rejected_before_any_api_call(self):
        for groups in ((("contract_id",),), (tuple(c.TERMS_SCHEMA["properties"]), ("contract_id",))):
            with self.assertRaisesRegex(ValueError, "exactly once"):
                common.request_json(h, {"task": "cashback_terms"}, c.TERMS_PROMPT, c.TERMS_SCHEMA,
                                    task="cashback_terms", max_tokens=16384, field_groups=groups)
        self.assertEqual(self.calls, [])

    def test_rejected_second_part_does_not_cache_an_incomplete_policy(self):
        self.reject_payment = True
        with self.assertRaisesRegex(RuntimeError, "HTTP 400.*union parameter limit is 16"):
            b.extract_policy(self.document, str(self.state), h)
        self.assertGreater(len(self.calls), 1)
        self.assertEqual(list((self.state / "extractions").glob("*.json")), [])

    def test_provider_error_is_visible_and_api_key_is_redacted(self):
        schema = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
        with self.assertRaises(RuntimeError) as raised:
            common.request_json(h, {"task": "reject"}, "Extract facts.", schema,
                                task="reject", max_tokens=100)
        message = str(raised.exception)
        self.assertIn("HTTP 400", message)
        self.assertIn("Schema compilation rejected", message)
        self.assertIn("request_id=req-schema-test", message)
        self.assertNotIn("test-schema-key", message)
        self.assertIn("[redacted]", message)

    def test_truncated_terms_retry_with_more_tokens_then_cache_only_complete_result(self):
        self.stop_reasons = ["max_tokens", "end_turn"]
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = w.extract_terms(self.document, {self.document["doc"]: self.document}, str(self.state), h)
        self.assertEqual(result, self.terms)
        self.assertEqual([call["max_tokens"] for call in self.calls], [16384, 32768])
        for field in ("model", "output_config", "system", "messages"):
            self.assertEqual(self.calls[0][field], self.calls[1][field])
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("retrying once", stderr.getvalue())
        caches = list((self.state / "extractions").glob("*.json"))
        self.assertEqual(len(caches), 1)
        self.assertEqual(json.loads(caches[0].read_text()), self.terms)
        count = len(self.calls)
        self.assertEqual(w.extract_terms(self.document, {self.document["doc"]: self.document}, str(self.state), h), self.terms)
        self.assertEqual(len(self.calls), count)

    def test_repeated_truncation_stops_after_one_retry_and_creates_no_cache(self):
        self.stop_reasons = ["max_tokens", "max_tokens", "end_turn"]
        with redirect_stderr(StringIO()), self.assertRaisesRegex(RuntimeError, r"max_tokens.*32768.*attempts=2"):
            w.extract_terms(self.document, {self.document["doc"]: self.document}, str(self.state), h)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(list((self.state / "extractions").glob("*.json")), [])

    def test_refusal_is_not_retried_or_cached(self):
        self.stop_reasons = ["refusal", "end_turn"]
        with self.assertRaisesRegex(RuntimeError, "refusal"):
            w.extract_terms(self.document, {self.document["doc"]: self.document}, str(self.state), h)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(list((self.state / "extractions").glob("*.json")), [])

    def test_already_at_token_ceiling_does_not_retry(self):
        self.stop_reasons = ["max_tokens", "end_turn"]
        with self.assertRaisesRegex(RuntimeError, r"max_tokens.*attempts=1"):
            common.request_json(h, {"task": "contract_terms"}, "Extract terms.", w.TERM_SCHEMA,
                                task="contract_terms", max_tokens=32768)
        self.assertEqual(len(self.calls), 1)

    def bad_policy_quotes(self):
        self.document["pages"].append("SEC Security screening = USD 2 per kg.\n"
                                      "Late fee 2 percent per month, grace 5 days.")
        reference = lambda quote: {"doc": self.document["doc"], "page": 2, "quote": quote}
        self.policy["definitions"] = [{"code": "SEC", "category": "security", "labels": ["Security screening"],
                                       "evidence": [reference("SEC Security screening = USD 2 per kg.")]}]
        self.policy["rules"] = [{"code": "SEC", "lane": None, "currency": "USD", "valid_from": None,
            "valid_to": None, "continues": None, "unit": "kg", "rate": "2", "minimum": None,
            "maximum": None, "base_codes": [], "free_days": None, "condition": "always",
            "evidence": [reference("SEC Security screening = USD 2 per kg.")]}]
        self.policy["payment"].update(monthly_rate="2", grace_days=5,
                                     evidence=[reference("Late fee 2 percent per month, grace 5 days.")])
        self.invalid_policy = copy.deepcopy(self.policy)
        self.invalid_policy["rules"][0]["evidence"][0]["quote"] = "Screening is two dollars per kg."
        self.invalid_policy["payment"]["evidence"][0]["quote"] = "Late fees incur 2% each month."

    def test_bad_policy_quotes_receive_field_and_source_feedback_then_cache_only_the_correction(self):
        self.bad_policy_quotes()
        self.assertEqual(b.extract_policy(self.document, str(self.state), h), self.policy)
        self.assertEqual(len(self.calls), 4)  # Two schema parts, then one corrective extraction.
        repairs = [json.loads(call["messages"][0]["content"]) for call in self.calls[2:]]
        for repair in repairs:
            self.assertEqual(repair["document"], self.document)
            self.assertEqual(repair["previous_result"], self.invalid_policy)
            error = repair["validation_error"]
            self.assertIn("billing_policy:", error)
            self.assertIn("$.rules[0].evidence[0]", error)
            self.assertIn("$.payment.evidence[0]", error)
            self.assertIn(self.document["doc"], error)
            self.assertIn("page=2", error)
            self.assertIn("Screening is two dollars", error)
            self.assertIn("Late fees incur 2%", error)
        caches = list((self.state / "extractions").glob("*.json"))
        self.assertEqual(len(caches), 1)
        self.assertEqual(json.loads(caches[0].read_text()), self.policy)
        with patch.object(h, "Anthropic", side_effect=AssertionError("Only a corrected cache may be used")):
            self.assertEqual(b.extract_policy(self.document, str(self.state), h), self.policy)

    def test_a_second_invented_quote_is_rejected_with_context_and_no_cache(self):
        self.bad_policy_quotes()
        self.policy = self.invalid_policy
        with self.assertRaisesRegex(ValueError, r"billing_policy:.*absent.*rules\[0\].*page=2"):
            b.extract_policy(self.document, str(self.state), h)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(list((self.state / "extractions").glob("*.json")), [])

    def test_invalid_cached_quotes_are_rejected_without_api_or_overwriting_the_cache(self):
        self.bad_policy_quotes()
        key = h.extraction_cache_key({"task": "billing_policy", "document": self.document},
                                     prompt=b.POLICY_PROMPT, schema=b.POLICY_SCHEMA)
        path = self.state / "extractions" / f"{key}.json"
        common.write_json(path, self.invalid_policy)
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "billing_policy:.*absent"):
            b.extract_policy(self.document, str(self.state), h)
        self.assertEqual(self.calls, [])
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()

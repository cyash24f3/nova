"""Opt-in end-to-end checks with real source mailboxes and the configured API."""
from __future__ import annotations

import argparse
import calendar
import copy
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from grader.score import amount_ok, key_tuple, names_match, range_ok, report_items
from grader.extract import Mailbox
from grader.validate import citation_errors, cli_errors, schema_errors
from live_mailboxes import build, build_feedback, build_authority
import yaml
from harness.confidence import MODEL_VERSION, metrics

EXAMPLES = {
    "expiry": "Contract expiry, 60-day alert window",
    "overcharge": "Freight, weight bands, minimum and fuel; clean control",
    "duplicate": "Paid invoice sent again; distinct house AWBs stay clean",
    "duplicate_rebilled_4_months": "Rebilling months later using a charge alias",
    "waived_but_billed": "Authorised waiver; clean control",
    "late_fees": "Due-date extension; a legitimate late fee stays clean",
    "cashback": "Assignment cashback example: closed-quarter qualifying spend",
    "conflict": "Assignment conflict example: two binding rates, including dependent fuel, USD 0-20.87",
}
SYNTHETIC = {
    "accessorial_fx": "kg/shipment/month, storage, eligibility and invoice-month FX",
    "percentage_minimum": "Minimum, percentage dependencies, cost pass-through, cap and injection",
    "payment_risk": "Undisputed principal and the 14-day reminder window",
    "compounding_extension": "Compounding monthly fees and an authorised extension",
    "credit_rebill": "Partial credit deducted from a later duplicate charge",
    "waiver_scope": "Conditional one-off waiver leaves another shipment alone",
    "missing_fx": "Missing FX month: request the named document from its holder",
    "cashback_rebate": "Quarter close, all-spend tiers, issued partial credit and claim deadline",
    "cashback_margin_annual": "Crossing-invoice margin step-down, partial credit and contract-year improvement",
}
FEEDBACK_CASES = {
    "feedback_learning": "Actual feedback CLI: close, reject, amount override and a separate later key",
    "feedback_rates": "Accepted rate notice: later clean/overbilled invoices and another lane",
    "feedback_authority": "Standing sender authority and a one-month approval stay within scope",
}
NEW_CASES = {
    "scanned_overcharge": "Local OCR and live extraction: recover USD 6 from a scanned invoice",
    "scanned_minimum_clean": "Local OCR and live extraction: minimum-charge invoice stays clean",
    "reordered_invoice": "Live extraction of a reordered invoice layout; USD 6 overcharge",
}
AUTHORITY_CASES = {
    "authority_precedence": "Signed amendment wins; unsigned, quoted, unauthorised and future changes stay out",
    "unranked_conflict": "Two binding unranked instruments: fuel dependency and USD 0-11 conflict range",
    "report_revisions": "Three real batches: supersedes, new retro key, retractions and historical replay",
}
CASES = {**EXAMPLES, **SYNTHETIC, **NEW_CASES, **FEEDBACK_CASES, **AUTHORITY_CASES}
# The assignment cashback MSA, section 7.3, gives the carrier 45 days after quarter end.
CASHBACK_EXAMPLE_CREDIT_DAYS = 45
PENDING = "none"
GUARDED_REPORT = """
import sys
sys.path.insert(0, sys.argv[1])
from harness import harness as h
def blocked(*args, **kwargs):
    print('Cache replay attempted an API call', file=sys.stderr, flush=True)
    raise AssertionError('Cache replay attempted an API call')
h.Anthropic = blocked
raise SystemExit(h.main(sys.argv[2:]))
"""


class CheckFailure(RuntimeError):
    pass


def golden_totals(expected: list[dict]) -> dict:
    totals = {"total_overpayment": Decimal(0), "cashback_owed": Decimal(0),
              "late_fees_avoided": Decimal(0)}
    for item in expected:
        if item.get("verdict") != "finding" or item.get("amount_usd") is None:
            continue
        field = {"cashback": "cashback_owed", "late_fee_risk": "late_fees_avoided"}.get(item["class"])
        if item["class"] == "expiry":
            continue
        totals[field or "total_overpayment"] += Decimal(str(item["amount_usd"]))
    totals["total_savings"] = sum(totals.values(), Decimal(0))
    return {field: f"{value:.2f}" for field, value in totals.items()}


def compare_answers(report: dict, expected: list[dict], *, mode="supervised") -> list[str]:
    """Compare decisions and money, allowing clean controls to be omitted."""
    errors = []
    actual = {key_tuple(item): item for item in report_items(report)}
    wanted = {key_tuple(item): item for item in expected}
    for key, goal in wanted.items():
        got = actual.get(key)
        label = " ".join(key)
        if goal["verdict"] == "no_finding":
            if got and (got.get("verdict") != "no_finding" or got.get("action") != "none"
                        or got.get("amount_usd") is not None):
                errors.append(f"Clean control was flagged: {label}")
            continue
        if got is None:
            errors.append(f"Missing expected item: {label}")
            continue
        for field in ("verdict", "deadline"):
            if field in goal and got.get(field) != goal[field]:
                errors.append(f"{label}: {field} expected {goal[field]!r}, got {got.get(field)!r}")
        action = goal["action"]
        if mode == "autonomous" and action.startswith("draft:"):
            explicit = goal.get("autonomous_action")
            checks = (got.get("action_basis") or {}).get("checks", {})
            check_names = {"evidence_complete", "no_conflict", "under_threshold", "reversible", "enough_authority"}
            eligible = set(checks) == check_names and all(value is True for value in checks.values())
            action = explicit or (action.replace("draft:", "execute:") if eligible else "human_review")
            recipient = (got.get("outbound") or {}).get("recipient") or {}
            if eligible and (not recipient.get("email") or not any(
                    recipient["email"].casefold() in ref["quote"].casefold() for ref in got.get("evidence", []))):
                errors.append(f"{label}: autonomous recipient lacks quoted Schedule 1 evidence")
        if got.get("action") != action:
            errors.append(f"{label}: action expected {action!r}, got {got.get('action')!r}")
        amount = goal.get("amount_usd")
        if (got.get("amount_usd") is not None if amount is None
                else not amount_ok(got.get("amount_usd"), amount)):
            errors.append(f"{label}: USD expected {amount!r}, got {got.get('amount_usd')!r}")
        if goal.get("amount_range") is not None and not range_ok(goal["amount_range"], got.get("amount_range")):
            errors.append(f"{label}: incorrect amount range")
        if goal.get("missing_doc"):
            missing = got.get("missing_doc") or {}
            if not names_match(goal["missing_doc"]["name"], missing.get("name")):
                errors.append(f"{label}: incorrect missing document name: {missing.get('name')!r}")
            holder = missing.get("holder") or {}
            for field, value in goal["missing_doc"]["holder"].items():
                words = lambda text: " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))
                if value and not names_match(words(value), words(holder.get(field))):
                    errors.append(f"{label}: incorrect document holder {field}: {holder.get(field)!r}")
    for key, got in actual.items():
        if key not in wanted and got.get("verdict") != "no_finding":
            errors.append(f"Unexpected item: {' '.join(key)} ({got.get('verdict')}, USD {got.get('amount_usd')})")
    for field, amount in golden_totals(expected).items():
        if not amount_ok((report.get("totals") or {}).get(field), amount):
            errors.append(f"{field}: expected {amount}, got {(report.get('totals') or {}).get(field)!r}")
    return errors


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def run_cli(arguments: list[str], *, cwd: Path, timeout: int, block_api=False) -> str:
    command = ([sys.executable, "-c", GUARDED_REPORT, str(ROOT)] if block_api else
               [sys.executable, str(ROOT / "harness/harness.py")]) + arguments
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise CheckFailure(f"{arguments[0]} exceeded {timeout}s; existing valid caches are preserved") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        # Provider errors already redact credentials; also redact any direct CLI traceback.
        for variable in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            secret = os.environ.get(variable)
            if secret:
                detail = detail.replace(secret, "[redacted]")
        raise CheckFailure(f"{arguments[0]} exited {result.returncode}: {detail[-4000:]}")
    return result.stdout


def read_report(state: Path, as_of: str, mode: str, *, cwd: Path, timeout: int, block_api=False) -> dict:
    output = run_cli(["report", "--as-of", as_of, "--mode", mode, "--state-dir", str(state)],
                     cwd=cwd, timeout=timeout, block_api=block_api)
    try:
        result = json.loads(output)
    except json.JSONDecodeError as exc:
        raise CheckFailure("Report stdout is not one valid JSON document") from exc
    if not isinstance(result, dict) or result.get("as_of") != as_of or result.get("mode") != mode:
        raise CheckFailure("Report has the wrong date, mode or top-level type")
    return result


def check_report(report: dict, expected: list[dict], mailbox: Path, *, mode="supervised") -> None:
    errors = schema_errors(report)
    if not errors:
        errors += citation_errors(report, mailbox)
        errors += compare_answers(report, expected, mode=mode)
    if errors:
        raise CheckFailure("\n".join(errors[:25]))


def check_confidence(report):
    factors = {"evidence_completeness", "source_authority", "field_extraction",
               "source_agreement", "clause_in_force"}
    observations = []
    for item in report_items(report):
        basis = item.get("confidence_basis") or {}
        values = basis.get("factors") or {}
        if basis.get("model") != MODEL_VERSION or set(values) != factors:
            raise CheckFailure(f"Missing confidence basis on {item['id']}")
        for value in [item["confidence"], *values.values()]:
            if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
                raise CheckFailure(f"Invalid confidence value on {item['id']}")
        # Called only after exact answer checks passed; don't label failed predictions as correct.
        observations.append((item["confidence"], True))
    return metrics(observations)


def load_new_case(name):
    case = yaml.safe_load((ROOT / "evals/cases" / f"own_{name}.yaml").read_text())
    expected = [{**item["key"], **{key: value for key, value in item.items() if key != "key"}}
                for item in case["checks"]]
    probes = [("2034-01-04", []), ("2034-01-05", copy.deepcopy(expected))]
    return ROOT / case["mailbox"], str(case["as_of"]), expected, probes


def load_example_case(name):
    example = ROOT / "data/examples" / name
    mailbox = example / "docs"
    golden = json.loads((example / "expected_finding.json").read_text())
    as_of, expected = golden["as_of"], report_items(golden)
    if name == "cashback":
        # The supplied example leaves this deadline null. Independently check the
        # original contract and calculate it; never take it from the harness output.
        clause = "Rebate credit notes are issued within 45 days after Quarter end."
        error = Mailbox(mailbox).check("batch_01/0002.eml#MSA-TBX-US-EXP-E58.pdf", 4, clause)
        if error:
            raise CheckFailure(f"Cashback deadline fixture does not match the contract: {error}")
        for item in expected:
            if item["class"] == "cashback" and item["credit_type"] == "rebate" and item["verdict"] == "finding":
                year, quarter = item["period"].split("-Q")
                month = int(quarter) * 3
                quarter_end = date(int(year), month, calendar.monthrange(int(year), month)[1])
                item["deadline"] = (quarter_end + timedelta(days=CASHBACK_EXAMPLE_CREDIT_DAYS)).isoformat()
    return mailbox, as_of, expected, date_probes(name, as_of, expected)


def date_probes(name: str, as_of: str, expected: list[dict]) -> list[tuple[str, list[dict]]]:
    if name == "cashback":
        rebate = next(item for item in expected if item["class"] == "cashback" and item["verdict"] == "finding")
        deadline = date.fromisoformat(rebate["deadline"])
        quarter_end = deadline - timedelta(days=CASHBACK_EXAMPLE_CREDIT_DAYS)
        tracked = {**copy.deepcopy(rebate), "action": "internal:track_credit"}
        return [(quarter_end.isoformat(), []), ((quarter_end + timedelta(days=1)).isoformat(), [tracked]),
                ((deadline - timedelta(days=1)).isoformat(), [tracked]), (deadline.isoformat(), [rebate])]
    if name != "expiry":
        return []
    expiry = next(item for item in expected if item["class"] == "expiry")
    deadline = date.fromisoformat(expiry["deadline"])
    return [((deadline - timedelta(days=61)).isoformat(), []),
            ((deadline - timedelta(days=60)).isoformat(), [expiry]),
            (deadline.isoformat(), [expiry]), ((deadline + timedelta(days=1)).isoformat(), [])]


def run_case(name: str, mailbox: Path, as_of: str, expected: list[dict], probes,
             output_dir: Path, *, state_override=None, timeout=600, cached_only=False,
             require_fresh_api=False, feedback_plan=None, revision_plan=None) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    state = state_override or output_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    batches = sorted(mailbox.glob("batch_*"))
    if not batches:
        raise CheckFailure(f"No source batches in {mailbox}")
    print(f"\n[{name}] {CASES[name]}", flush=True)
    print(f"  State: {state}", flush=True)
    before = len(list(state.glob("extractions/*.json")))
    if require_fresh_api and (cached_only or before):
        raise CheckFailure("Fresh API verification requires an empty extraction cache and API access")
    staged = []
    for stage in revision_plan or []:
        batch = mailbox / stage["batch"]
        run_cli(["ingest", str(batch), "--state-dir", str(state)], cwd=output_dir, timeout=timeout)
        current = read_report(state, stage["as_of"], "supervised", cwd=output_dir, timeout=timeout, block_api=cached_only)
        check_report(current, stage["expected"], mailbox)
        check_revisions(staged[-1][1] if staged else None, current, stage)
        write_json(output_dir / f"stage-{stage['batch']}.json", current)
        staged.append((stage, current))
    if staged:
        print("  PASS staged arrivals, supersedes, retro keys and proven retractions", flush=True)
    for batch in batches:
        run_cli(["ingest", str(batch), "--state-dir", str(state)], cwd=output_dir, timeout=timeout)
    snapshot = {str(path.relative_to(state)): path.read_bytes() for path in state.glob("*/documents.json")}
    for batch in batches:
        run_cli(["ingest", str(batch), "--state-dir", str(state)], cwd=output_dir, timeout=timeout)
    if snapshot != {str(path.relative_to(state)): path.read_bytes() for path in state.glob("*/documents.json")}:
        raise CheckFailure("Re-ingesting the same batches changed stored documents")
    print(f"  PASS ingestion and repeat ingestion ({len(batches)} batch(es))", flush=True)
    print("  Running supervised report (API blocked) ..." if cached_only else
          "  Running supervised report (API permitted; valid caches reused) ...", flush=True)
    report = read_report(state, as_of, "supervised", cwd=output_dir, timeout=timeout, block_api=cached_only)
    if feedback_plan:
        # A reused state may already have these exact decisions. Historical cutoff always excludes them.
        before_report = read_report(state, "2034-02-09", "supervised", cwd=output_dir, timeout=timeout, block_api=cached_only)
        check_report(before_report, feedback_plan["before"], mailbox)
        write_json(output_dir / "before-feedback.json", before_report)
        feedback_file = output_dir / "feedback-input.json"
        write_json(feedback_file, feedback_plan["feedback"])
        run_cli(["feedback", str(feedback_file), "--state-dir", str(state)], cwd=output_dir, timeout=timeout)
        snapshot = (state / "feedback.json").read_bytes()
        run_cli(["feedback", str(feedback_file), "--state-dir", str(state)], cwd=output_dir, timeout=timeout)
        if (state / "feedback.json").read_bytes() != snapshot:
            raise CheckFailure("Repeating feedback changed persistent decision memory")
        report = read_report(state, as_of, "supervised", cwd=output_dir, timeout=timeout, block_api=cached_only)
        actual = {key_tuple(item): item for item in report_items(report)}
        for key, item_id in feedback_plan["learned"]:
            item = actual.get(key_tuple(key))
            if item is None or item.get("learned_at") != {"feedback_item_id": item_id, "batch": "batch_01"}:
                raise CheckFailure(f"Missing feedback provenance on {key}")
        print("  PASS real feedback CLI, persistent/idempotent decisions and learned_at", flush=True)
    write_json(output_dir / "report.json", report)
    check_report(report, expected, mailbox)
    confidence_result = check_confidence(report)
    if name in (*SYNTHETIC, *NEW_CASES, *FEEDBACK_CASES, *AUTHORITY_CASES) and report.get("diagnostics"):
        raise CheckFailure(f"Unexpected diagnostic in complete synthetic sources: {report['diagnostics']}")
    after = len(list(state.glob("extractions/*.json")))
    if require_fresh_api and after == 0:
        raise CheckFailure("No fresh API extraction was saved; live API use was not verified")
    print(f"  PASS expected decisions, USD amounts, actions, totals, schema and citations; {after - before} new cache files", flush=True)
    if require_fresh_api:
        print("  PASS fresh API extractions verified (initial extraction cache was empty)", flush=True)
    print("  PASS evidence-based confidence; " + json.dumps(confidence_result, sort_keys=True), flush=True)
    replay = read_report(state, as_of, "supervised", cwd=output_dir, timeout=timeout, block_api=True)
    if replay != report:
        raise CheckFailure("Fresh-process cache replay changed the report")
    print("  PASS identical fresh-process replay with API disabled", flush=True)
    autonomous = read_report(state, as_of, "autonomous", cwd=output_dir, timeout=timeout, block_api=True)
    write_json(output_dir / "autonomous.json", autonomous)
    check_report(autonomous, expected, mailbox, mode="autonomous")
    print("  PASS autonomous execution checks, holds and internal actions", flush=True)
    documents = [doc for path in state.glob("*/documents.json") for doc in json.loads(path.read_text())]
    if name in NEW_CASES:
        scans = [doc for doc in documents if any(source["method"] == "ocr" for source in doc.get("page_sources", []))]
        if name.startswith("scanned_"):
            if not scans or any(not all(page.strip() for page in doc["pages"]) for doc in scans):
                raise CheckFailure("The scanned invoice was not read by local OCR")
            print("  PASS scanned PDF has readable OCR pages and recognition provenance", flush=True)
        elif scans:
            raise CheckFailure("Native reordered invoice unexpectedly needed OCR")
    earliest = min(datetime.fromisoformat(doc["received_at"].replace("Z", "+00:00"))
                   .astimezone(timezone.utc).date() for doc in documents)
    probes = list(probes) + [((earliest - timedelta(days=1)).isoformat(), [])]
    for probe_date, probe_expected in probes:
        probe = read_report(state, probe_date, "supervised", cwd=output_dir, timeout=timeout, block_api=True)
        write_json(output_dir / f"report-{probe_date}.json", probe)
        check_report(probe, probe_expected, mailbox)
    print(f"  PASS {len(probes)} date checks with API disabled (including future-document exclusion)", flush=True)
    for stage, saved in staged:
        replay = read_report(state, stage["as_of"], "supervised", cwd=output_dir, timeout=timeout, block_api=True)
        if replay != saved:
            raise CheckFailure(f"Historical stage {stage['batch']} changed after later documents arrived")
    if staged:
        print("  PASS exact historical stage replay after later batches with API disabled", flush=True)
    # Return the persisted document index to the primary report's cutoff.
    restored = read_report(state, as_of, "supervised", cwd=output_dir, timeout=timeout, block_api=True)
    if restored != report:
        raise CheckFailure("Reporting an earlier cutoff changed the later report")
    return {"case": name, "status": "PASS", "state_dir": str(state),
            "report": str(output_dir / "report.json"), "new_cache_files": after - before,
            "date_checks": len(probes), "totals": report["totals"], "confidence": confidence_result,
            "fresh_api_verified": require_fresh_api and before == 0 and after > 0}


def check_revisions(previous, current, stage):
    old = {key_tuple(item): item for item in report_items(previous)} if previous else {}
    actual = {key_tuple(item): item for item in report_items(current)}
    for key in stage.get("revisions", []):
        prior, item = old.get(key_tuple(key)), actual.get(key_tuple(key))
        if not prior or not item or item["id"] == prior["id"] or item.get("supersedes") != [prior["id"]]:
            raise CheckFailure(f"Missing precise supersedes link for {key}")
        if not any(ref["doc"].startswith(stage["batch"] + "/") for ref in item["evidence"]):
            raise CheckFailure(f"Revision does not cite the new batch for {key}")
    for key in stage.get("retro", []):
        item = actual.get(key_tuple(key))
        if key_tuple(key) in old or not item or item.get("retro") is not True or item.get("supersedes") != []:
            raise CheckFailure(f"New backdated key must be retro without supersedes: {key}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--case", choices=list(CASES))
    selection.add_argument("--all", action="store_true", help="Stop on first failure; reruns reuse valid caches")
    selection.add_argument("--list", action="store_true", help="List implemented-feature coverage")
    parser.add_argument("--out-root", type=Path, default=ROOT / "out/state_live_checks")
    parser.add_argument("--state-dir", type=Path, help="Reuse a state directory; only allowed with --case")
    parser.add_argument("--cached-only", action="store_true", help="Forbid all API requests, including the first report")
    parser.add_argument("--fresh-api", action="store_true",
                        help="use a new saved run folder and require fresh API extractions; existing caches remain intact")
    parser.add_argument("--timeout", type=int, default=600, help="Seconds allowed per harness process")
    args = parser.parse_args(argv)
    if args.state_dir and not args.case:
        parser.error("--state-dir requires --case so mailboxes never share a state directory")
    if args.fresh_api and (args.cached_only or args.state_dir):
        parser.error("--fresh-api cannot be combined with --cached-only or --state-dir")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.list:
        for name, description in CASES.items():
            print(f"{name}: {description}")
        print(f"Pending features, excluded: {PENDING}.")
        return 0
    if not args.cached_only and not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("FAIL: API credentials missing in this terminal. Use the terminal where your key is configured.\n"
              "Use --cached-only to replay already completed extractions without any API requests.")
        return 1
    errors = cli_errors(ROOT / "harness/harness.py")
    if errors:
        print("FAIL CLI interface: " + "; ".join(errors))
        return 1
    print("PASS CLI argument/help contract", flush=True)
    from harness import harness as backend
    print(f"Configured model: {backend.CLAUDE_MODEL}; effort: {backend.CLAUDE_EFFORT}", flush=True)
    if args.cached_only:
        print("All API requests are blocked.", flush=True)
    else:
        print("Fresh extractions use your configured Anthropic API and incur its normal charges.\n"
              "Replay, mode and date checks block the API.", flush=True)
        print("Fresh API verification: a new saved state folder is used; earlier caches are kept." if args.fresh_api else
              "Rerunning reuses valid extraction caches.", flush=True)
    output_root = args.out_root.expanduser().resolve()
    state_override = args.state_dir.expanduser().resolve() if args.state_dir else None
    results = []
    for name in ([args.case] if args.case else CASES):
        started = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="nova-live-sources-") as temporary:
                feedback_plan = revision_plan = None
                if name in EXAMPLES:
                    mailbox, as_of, expected, probes = load_example_case(name)
                elif name in NEW_CASES:
                    mailbox, as_of, expected, probes = load_new_case(name)
                elif name in FEEDBACK_CASES:
                    mailbox = Path(temporary)
                    as_of, expected, probes, feedback_plan = build_feedback(name, mailbox)
                elif name in AUTHORITY_CASES:
                    mailbox = Path(temporary)
                    as_of, expected, probes, revision_plan = build_authority(name, mailbox)
                else:
                    mailbox = Path(temporary)
                    as_of, expected, probes = build(name, mailbox)
                output_dir = output_root / name
                if args.fresh_api:
                    output_dir = output_dir / f"fresh-{time.time_ns()}"
                result = run_case(name, mailbox, as_of, copy.deepcopy(expected), probes,
                                  output_dir, state_override=state_override,
                                  timeout=args.timeout, cached_only=args.cached_only,
                                  require_fresh_api=args.fresh_api, feedback_plan=feedback_plan, revision_plan=revision_plan)
        except (CheckFailure, OSError, ValueError) as exc:
            result = {"case": name, "status": "FAIL", "error": str(exc)}
            print(f"FAIL [{name}]: {exc}\nStopped here. Fix this failure before running further cases.", flush=True)
        result["seconds"] = round(time.monotonic() - started, 2)
        results.append(result)
        write_json(output_root / name / "check.json", result)
        write_json(output_root / "results.json", {"results": results, "excluded_pending": PENDING})
        if result["status"] == "FAIL":
            return 1
    print(f"\nPASS {len(results)} implemented-feature case(s). Results: {output_root / 'results.json'}", flush=True)
    print("All assignment features are implemented; full practice/exam evaluation and submission checks are separate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

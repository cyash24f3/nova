"""Run your eval cases (evals/cases/*.yaml) through your harness and score them. Behind `make eval`.

    python evals/run_evals.py [--harness harness/harness.py] [--cases evals/cases] [--out evals/results.json] [-k name]

Each case gets a fresh state folder: every batch_* of its mailbox is ingested in order (with a feedback file after
the batch it names, if the case has one), then one report on the case's date and mode. The item with the case's key
is compared with `expect`, using the grader's own matching (grader/score.py): verdict, amount within USD 0.01, the
range for a conflict, the action, and the missing document's name for cannot_determine. `verdict: no_finding` passes
when the key is missing or listed as no_finding. Exit code 0 when every case passes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "grader"))
sys.path.insert(0, str(ROOT / "tools"))
import score as S  # noqa: E402
from run_mailbox import run  # noqa: E402
from grader.validate import citation_errors, schema_errors  # noqa: E402
from harness.confidence import metrics, raw_probability  # noqa: E402

CACHE_GUARD = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location('cached_eval', sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
def blocked(*args, **kwargs):
    raise RuntimeError('API blocked: no validated extraction cache for this request')
module.Anthropic = blocked
raise SystemExit(module.main(sys.argv[2:]))
"""


def find_item(report: dict, key: dict) -> dict | None:
    want = S.key_tuple(key)
    for it in S.report_items(report):
        if S.key_tuple(it) == want:
            return it
    return None


def check(case: dict, item: dict | None) -> list[str]:
    """-> what differs (empty when the case passes)."""
    exp = case["expect"]
    v = exp.get("verdict", "finding")
    if v == "no_finding" and item is None:
        return []
    if item is None:
        return ["key missing from the report"]
    bad = []
    if item.get("verdict") != v:
        bad.append(f"verdict {item.get('verdict')}, expected {v}")
    if "amount_usd" in exp:
        correct = item.get("amount_usd") is None if exp["amount_usd"] is None else S.amount_ok(item.get("amount_usd"), exp["amount_usd"])
        if not correct:
            bad.append(f"amount {item.get('amount_usd')}, expected {exp['amount_usd']}")
    if exp.get("amount_range") and not S.range_ok(item.get("amount_range"), exp["amount_range"]):
        bad.append(f"range {item.get('amount_range')}, expected {exp['amount_range']}")
    if exp.get("action") and str(item.get("action")) != exp["action"]:
        bad.append(f"action {item.get('action')}, expected {exp['action']}")
    if "deadline" in exp and str(item.get("deadline")) != str(exp["deadline"]):
        bad.append(f"deadline {item.get('deadline')}, expected {exp['deadline']}")
    want_doc = (exp.get("missing_doc") or {}).get("name") if isinstance(exp.get("missing_doc"), dict) else exp.get("missing_doc")
    if want_doc:
        got = ((item.get("missing_doc") or {}).get("name") or "").lower()
        if not all(w in got for w in str(want_doc).lower().split()):
            bad.append(f"missing_doc {got or 'none'}, expected {want_doc}")
        if isinstance(exp.get("missing_doc"), dict):
            holder = (item.get("missing_doc") or {}).get("holder") or {}
            for field, value in (exp["missing_doc"].get("holder") or {}).items():
                tokens = lambda text: set(re.findall(r"[a-z0-9]+", str(text or "").casefold()))
                if not tokens(value) or not tokens(value) <= tokens(holder.get(field)):
                    bad.append(f"missing document holder {field}: {holder.get(field)}, expected {value}")
    if "learned_at" in exp and item.get("learned_at") != exp["learned_at"]:
        bad.append(f"learned_at {item.get('learned_at')}, expected {exp['learned_at']}")
    return bad


def check_report(case, report, mailbox):
    bad = schema_errors(report)
    if bad:
        return bad
    bad += citation_errors(report, mailbox)
    expectations = case.get("checks", [case["expect"]])
    for expectation in expectations:
        bad += check({"expect": expectation}, find_item(report, expectation["key"]))
    if "checks" in case:
        allowed = {S.key_tuple(expectation["key"]) for expectation in expectations}
        for item in S.report_items(report):
            if item.get("verdict") != "no_finding" and S.key_tuple(item) not in allowed:
                bad.append(f"unexpected reported item: {S.key_tuple(item)}")
    return bad


def confidence_labels(case, report):
    expected = {S.key_tuple(exp["key"]): exp for exp in case.get("checks", [case["expect"]])}
    labels = []
    for item in S.report_items(report):
        exp = expected.get(S.key_tuple(item))
        if exp is None and "checks" not in case:
            continue  # A sample with a single labelled key doesn't label the rest of its portfolio.
        # Probabilities predict verdict and money. Actions are scored separately.
        target = {key: value for key, value in (exp or {}).items() if key not in ("action", "deadline", "learned_at")}
        correct = exp is not None and not check({"expect": target}, item)
        labels.append((S.key_tuple(item), float(item["confidence"]), correct))
    return labels


def mailbox_cache(root, mailbox):
    # Keep dependent extractions with the policy/catalog that produced their inputs.
    # Two valid LLM reads of the same clause can use different equivalent labels.
    return root / hashlib.sha256(str(mailbox.resolve()).encode()).hexdigest()[:16]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="run_evals.py", description="score your eval cases")
    ap.add_argument("--harness", default="harness/harness.py")
    ap.add_argument("--cases", default=str(ROOT / "evals" / "cases"))
    ap.add_argument("--out", default=str(ROOT / "evals" / "results.json"))
    ap.add_argument("-k", help="only cases whose file name contains this")
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--cache-dir", type=Path, default=ROOT / "out/state_custom_evals/extractions",
                    help="reuse only validated extraction results; each case still has fresh document state")
    ap.add_argument("--cached-only", action="store_true", help="block all API calls; cache misses fail")
    a = ap.parse_args(argv)
    files = sorted(Path(a.cases).glob("*.yaml"))
    if a.k:
        files = [f for f in files if a.k in f.name]
    if not files:
        print(f"no cases in {a.cases}", file=sys.stderr)
        return 2
    results, observations, calibration_observations = [], {}, {}
    harness = a.harness
    if a.cached_only:
        path = Path(harness)
        if path.suffix != ".py" or not path.is_file():
            ap.error("--cached-only requires a Python harness file")
        harness = shlex.join([sys.executable, "-c", CACHE_GUARD, str(path.resolve())])
    for f in files:
        case = yaml.safe_load(f.read_text())
        mailbox = (ROOT / case["mailbox"]) if not Path(case["mailbox"]).is_absolute() else Path(case["mailbox"])
        fb = {k: (ROOT / v) for k, v in (case.get("feedback") or {}).items()}   # {batch_02: path/to/feedback.json}
        with tempfile.TemporaryDirectory(prefix="eval-") as tmp:
            report, errors = run(harness, mailbox, str(case["as_of"]), case.get("mode", "supervised"),
                                 Path(tmp) / "state", a.timeout, fb, log=lambda *_: None,
                                 extraction_cache=mailbox_cache(a.cache_dir, mailbox))
        diff = list(errors)
        if report is not None:
            diff += check_report(case, report, mailbox)
            if not schema_errors(report):
                reported_items = {S.key_tuple(item): item for item in S.report_items(report)}
                for key, probability, correct in confidence_labels(case, report):
                    observations[(str(mailbox), str(case["as_of"]), case.get("mode", "supervised"), key)] = (probability, correct)
                    item = reported_items.get(key)
                    factors = (item.get("confidence_basis") or {}).get("factors") if item else None
                    if factors and f.name.startswith("own_"):
                        # One observation per key/cutoff, shared across modes and
                        # date-window cases of the same physical mailbox.
                        calibration_observations[(str(mailbox), str(case["as_of"]), key)] = {
                            "group": hashlib.sha256(str(mailbox.resolve()).encode()).hexdigest(),
                            "raw_probability": raw_probability(factors), "correct": correct}
        ok = not diff
        results.append({"case": f.name, "name": case.get("name"), "pass": ok, "diff": diff})
        print(f"{'PASS' if ok else 'FAIL'}  {f.name}: {case.get('name', '')}" + ("" if ok else f"\n      {'; '.join(diff)[:400]}"))
    n = sum(r["pass"] for r in results)
    ours = sum(1 for r in results if r["case"].startswith("sample_"))
    summary = {"harness": a.harness, "passed": n, "total": len(results), "your_cases": len(results) - ours,
               "score": round(n / len(results), 4), "cases": results,
               "execution": "validated_cache_only" if a.cached_only else "api_with_validated_cache",
               "confidence": metrics(list(observations.values())),
               "confidence_observations": list(calibration_observations.values())}
    if not a.k:                                  # a filtered run never overwrites the full results
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(summary, indent=1) + "\n")
    print("Confidence: " + json.dumps(summary["confidence"], sort_keys=True))
    print(f"\n{n} of {len(results)} cases pass ({len(results) - ours} of them yours)" + ("" if a.k else f"; wrote {a.out}"))
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

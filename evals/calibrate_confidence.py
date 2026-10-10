"""Fit confidence from saved own-case reports; this command never calls an API."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from evals.run_evals import confidence_labels
from harness import common, confidence
from live_checks import SYNTHETIC, NEW_CASES, FEEDBACK_CASES, AUTHORITY_CASES, load_new_case
from live_mailboxes import build, build_feedback, build_authority
from grader.score import key_tuple, report_items


def saved_observations(root):
    observations, sources = [], []
    # Company examples and practice/exam answer files are deliberately excluded.
    for name in {**SYNTHETIC, **NEW_CASES, **FEEDBACK_CASES, **AUTHORITY_CASES}:
        directory = root / name
        check = directory / "check.json"
        selected = json.loads(check.read_text()).get("report") if check.is_file() else None
        path = Path(selected) if selected else directory / "report.json"
        if not path.is_file():
            continue
        report = json.loads(path.read_text())
        with tempfile.TemporaryDirectory() as temporary:
            mailbox = Path(temporary)
            if name in NEW_CASES:
                _, as_of, expected, _ = load_new_case(name)
            elif name in FEEDBACK_CASES:
                as_of, expected, _, _ = build_feedback(name, mailbox)
            elif name in AUTHORITY_CASES:
                as_of, expected, _, _ = build_authority(name, mailbox)
            else:
                as_of, expected, _ = build(name, mailbox)
        if report["as_of"] != as_of or report["mode"] != "supervised":
            raise ValueError(f"Saved report has the wrong date or mode: {path}")
        expectations = [{"key": {key: item[key] for key in
                         ("class", "invoice_id", "contract_id", "period", "credit_type") if key in item},
                         **{key: value for key, value in item.items() if key not in ("id", "evidence")}}
                        for item in expected]
        case = {"checks": expectations, "expect": expectations[0]}
        labels = {key: correct for key, _, correct in confidence_labels(case, report)}
        scored = skipped = 0
        for item in report_items(report):
            factors = (item.get("confidence_basis") or {}).get("factors")
            if factors is None:
                # Older reports predate confidence factors. They cannot train
                # this model without fabricating inputs or re-extracting data.
                skipped += 1
                continue
            observations.append({"group": name, "raw_probability": confidence.raw_probability(factors),
                                 "correct": labels[key_tuple(item)]})
            scored += 1
        sources.append({"case": name, "report_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "scored_items": scored, "skipped_without_factors": skipped})
    return observations, sources


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--saved-live-dir", type=Path, default=ROOT / "out/state_live_checks")
    parser.add_argument("--observations", type=Path, help="alternative own-eval observation JSON")
    parser.add_argument("--out", type=Path, default=confidence.CALIBRATION_PATH)
    parser.add_argument("--regularization", type=float, default=4.)
    args = parser.parse_args(argv)
    if args.observations:
        loaded = json.loads(args.observations.read_text())
        observations, sources = (loaded["confidence_observations"] if isinstance(loaded, dict) else loaded), []
    else:
        observations, sources = saved_observations(args.saved_live_dir)
    model = confidence.fit_calibration(observations, regularization=args.regularization)
    model["provenance"] = {"source": "own_eval_observations" if args.observations else "saved_own_live_checks",
                           "api_calls": 0, "reports": sources,
                           "observations_sha256": hashlib.sha256(json.dumps(observations, sort_keys=True).encode()).hexdigest()}
    common.write_json(args.out, model)
    print(f"Scored {model['observations']} items from {model['groups']} own mailbox groups: "
          f"{model['correct']} correct, {model['incorrect']} incorrect.")
    print(f"Held-out Brier: {model['validation']['baseline']['brier']} -> {model['validation']['calibrated']['brier']}.")
    print(f"Calibration {'accepted' if model['accepted'] else 'rejected; original probabilities retained'}. Saved: {args.out}")
    if model["incorrect"] == 0 or model["correct"] == 0:
        print("Only one correctness label is present; AUROC cannot be measured. No incorrect outcomes were invented.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

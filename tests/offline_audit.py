"""Replay the unmodified practice grader with saved extractions and no API access.

By default this produces a separate diagnostic card. --grade runs actual make
grade to write the official card using the same guarded cache replay.
Only extraction caches are reused: documents, feedback and history start empty.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from grader import run_grader as runner, score


def audit_live(output):
    from live_checks import CASES
    results = []
    for name in CASES:
        manifest = ROOT / "out/state_live_checks" / name / "check.json"
        if not manifest.is_file():
            results.append({"case": name, "status": "UNVERIFIED", "reason": "No saved API run"})
            continue
        source = Path(json.loads(manifest.read_text())["state_dir"])
        with tempfile.TemporaryDirectory(prefix="nova-offline-live-") as temporary:
            state = Path(temporary) / "state"
            for folder in ("extractions", "ocr"):
                if (source / folder).is_dir():
                    shutil.copytree(source / folder, state / folder)
            command = [sys.executable, str(ROOT / "tests/live_checks.py"), "--case", name,
                       "--cached-only", "--state-dir", str(state), "--out-root", str(output / "live")]
            result = subprocess.run(command, capture_output=True, text=True, cwd=ROOT, timeout=600)
            log = result.stdout + result.stderr
            (output / "live" / f"{name}.log").write_text(log)
            status = "PASS" if result.returncode == 0 else (
                "UNVERIFIED" if "Cache replay attempted an API call" in log else "FAIL")
            record = {"case": name, "status": status}
            if result.returncode:
                record["error"] = log[-2400:]
                if status == "UNVERIFIED":
                    record["reason"] = "Saved cache does not cover the current request; API access stayed blocked"
            results.append(record)
            print(f"Cached live replay {name}: {record['status']}", flush=True)
    (output / "live_results.json").write_text(json.dumps(results, indent=2) + "\n")
    return all(item["status"] == "PASS" for item in results)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=ROOT / "out/state_practice_debug/extractions")
    parser.add_argument("--out", type=Path, default=ROOT / "out/offline_audit")
    execution = parser.add_mutually_exclusive_group()
    execution.add_argument("--live", action="store_true", help="Replay all saved live cases, with API requests blocked")
    execution.add_argument("--grade", action="store_true",
                           help="Run actual make grade in both modes using saved extractions; block all new API requests")
    args = parser.parse_args(argv)
    cache = args.cache.resolve()
    output = args.out.resolve()
    if not any(cache.glob("*.json")):
        parser.error(f"No saved extraction results in {cache}")
    output.mkdir(parents=True, exist_ok=True)
    if args.live:
        (output / "live").mkdir(parents=True, exist_ok=True)
        return 0 if audit_live(output) else 1
    data = ROOT / "data/scenarios"
    key = score.load_key(data / "answers", data)
    with tempfile.TemporaryDirectory(prefix="nova-offline-audit-") as temporary:
        wrapper = Path(temporary) / "offline_harness.py"
        wrapper.write_text(f'''
import shutil
import sys
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
from harness import harness as h, common

def blocked(*args, **kwargs):
    raise RuntimeError("API access blocked by offline audit")

def missing(backend, request, prompt, schema, *, task, max_tokens):
    document = request.get("document", request)
    raise RuntimeError("Missing saved extraction: " + task + " " + str(document.get("doc", "")))

h.Anthropic = blocked
common._send_json = missing
if "--state-dir" in sys.argv:
    state = Path(sys.argv[sys.argv.index("--state-dir") + 1])
    target = state / "extractions"
    target.mkdir(parents=True, exist_ok=True)
    for source in Path({str(cache)!r}).glob("*.json"):
        destination = target / source.name
        if not destination.exists():
            shutil.copy2(source, destination)
sys.exit(h.main())
''', encoding="utf-8")
        if args.grade:
            print("Running make grade with saved API extractions; all new API requests are blocked.", flush=True)
            return subprocess.run(["make", "grade", f"HARNESS={wrapper}", "MODE=both"], cwd=ROOT).returncode
        runs, delivered = runner.replay(
            [sys.executable, str(wrapper)], data, key,
            ["supervised", "autonomous"], 600,
            reports_dir=output / "reports",
        )
    card = score.score(key, runs, runner.mailboxes(data, key), delivered)
    card["verification"] = {"kind": "cached_offline_replay", "api_calls": 0,
                            "cache_files": len(list(cache.glob("*.json")))}
    card["runs"] = runs
    (output / "card.json").write_text(json.dumps(card, indent=2, default=str) + "\n")
    (output / "card.txt").write_text(score.page(card) + "\n")
    print(score.page(card))
    return 0 if all(result.get("pass") for result in card["sheets"].values()) and not card["capped_by_hard_gate"] else 1


if __name__ == "__main__":
    sys.exit(main())

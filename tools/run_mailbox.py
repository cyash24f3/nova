"""Run your harness over one mailbox and keep the report. Behind `make sheet` and `make run`.

    python tools/run_mailbox.py --data data/exam --out out/exam_report.json --sheet answer_sheet.json
    python tools/run_mailbox.py --data data/scenarios --out out/practice_report.json

It starts from an empty state folder, runs `ingest` on every batch_* folder in order, then one `report` on the last
batch's date from <data>/batches.json (or --as-of). With --sheet it turns that report into answer_sheet.json with
grader/make_sheet.py. Each step is a fresh process, as in the grader. --resume keeps
the existing state and valid extractions after a failed run.
"""
from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "grader"))


def harness_cmd(harness: str) -> list[str]:
    cmd = shlex.split(harness)
    if cmd and cmd[0].endswith(".py"):
        cmd = [sys.executable] + cmd
    elif cmd and cmd[0] in ("python", "python3"):
        cmd[0] = sys.executable
    return cmd


def step(cmd: list[str], args: list[str], timeout: float) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd + args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout:.0f} s"


def batch_dates(data: Path) -> dict[str, str]:
    f = data / "batches.json"
    if not f.is_file():
        return {}
    return {b["batch"]: b["as_of"] for b in json.loads(f.read_text())["batches"]}


def run(harness: str, mailbox: Path, as_of: str, mode: str, state: Path, timeout: float = 600.0,
        feedback: dict[str, Path] | None = None, log=print,
        extraction_cache: Path | None = None, resume: bool = False) -> tuple[dict | None, list[str]]:
    """Ingest every batch, then report; fresh state by default, preserved with resume."""
    cmd = harness_cmd(harness)
    if resume and not state.is_dir():
        return None, [f"cannot resume: state folder {state} does not exist"]
    if state.exists() and not resume:
        shutil.rmtree(state)
    state.mkdir(parents=True, exist_ok=True)
    errors = []
    batches = sorted(p for p in mailbox.glob("batch_*") if p.is_dir())
    if not batches:
        return None, [f"no batch_* folders in {mailbox}"]
    for b in batches:
        rc, _, err = step(cmd, ["ingest", str(b), "--state-dir", str(state)], timeout)
        log(f"  ingest {b.name}: exit {rc}")
        if rc != 0:
            errors.append(f"ingest {b.name} exited {rc}: {err.strip()[-400:]}")
        fb = (feedback or {}).get(b.name)
        if fb is not None:
            rc, _, err = step(cmd, ["feedback", str(fb), "--state-dir", str(state)], timeout)
            log(f"  feedback after {b.name}: exit {rc}")
            if rc != 0:
                errors.append(f"feedback {fb} exited {rc}: {err.strip()[-400:]}")
    if extraction_cache is not None:
        target = state / "extractions"
        target.mkdir(exist_ok=True)
        for cached in extraction_cache.glob("*.json"):
            shutil.copy2(cached, target / cached.name)
    rc, out, err = step(cmd, ["report", "--as-of", as_of, "--mode", mode, "--state-dir", str(state)], timeout)
    if extraction_cache is not None:
        extraction_cache.mkdir(parents=True, exist_ok=True)
        for cached in (state / "extractions").glob("*.json"):
            destination = extraction_cache / cached.name
            if destination.exists() and destination.read_bytes() != cached.read_bytes():
                errors.append(f"extraction cache disagrees for {cached.name}")
            else:
                shutil.copy2(cached, destination)
    log(f"  report --as-of {as_of} --mode {mode}: exit {rc}")
    if rc != 0:
        errors.append(f"report exited {rc}: {err.strip()[-400:]}")
        return None, errors
    try:
        return json.loads(out), errors
    except json.JSONDecodeError as e:
        errors.append(f"report did not print JSON: {e}")
        return None, errors


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="run_mailbox.py", description=__doc__.split("\n")[0])
    ap.add_argument("--data", required=True, help="the mailbox: a folder holding batch_01/, batch_02/ ...")
    ap.add_argument("--harness", default="harness/harness.py")
    ap.add_argument("--as-of", help="report date (default: the last date in <data>/batches.json)")
    ap.add_argument("--mode", choices=["supervised", "autonomous"], default="supervised")
    ap.add_argument("--out", required=True, help="where to write the report.json")
    ap.add_argument("--sheet", help="also write the answer sheet here")
    ap.add_argument("--state-dir", help="default: out/state_<mailbox name>")
    ap.add_argument("--resume", action="store_true", help="preserve the existing state and valid extraction caches")
    ap.add_argument("--timeout", type=float, default=600.0, help="seconds per step")
    a = ap.parse_args(argv)
    data = Path(a.data)
    dates = batch_dates(data)
    as_of = a.as_of or (dates[max(dates)] if dates else None)
    if not as_of:
        print(f"run_mailbox: no --as-of and no {data}/batches.json", file=sys.stderr)
        return 2
    state = Path(a.state_dir or f"out/state_{data.name}")
    print(f"running {a.harness} on {data} (report as of {as_of}, {a.mode})")
    report, errors = run(a.harness, data, as_of, a.mode, state, a.timeout, resume=a.resume)
    for e in errors:
        print(f"harness error: {e}", file=sys.stderr)
    if report is None:
        print("no report: fix the errors above and run it again", file=sys.stderr)
        return 1
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1) + "\n")
    print(f"wrote {out}")
    if a.sheet:
        from make_sheet import make_sheet
        sheet = make_sheet(report, source=out.name)
        sheet["mailbox"] = data.name
        Path(a.sheet).write_text(json.dumps(sheet, indent=1) + "\n")
        t = sheet["totals"]
        print(f"wrote {a.sheet}: total_savings {t['total_savings']} (overpayment {t['total_overpayment']}, cashback "
              f"{t['cashback_owed']}, late fees avoided {t['late_fees_avoided']}); {len(sheet['conflicts'])} "
              f"conflict(s), {len(sheet['cannot_determine'])} cannot_determine")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

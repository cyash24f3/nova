# My solution

The harness uses Claude to extract document meaning into validated facts, then applies the glossary's arithmetic,
authority, date and action rules in Python. State persists between CLI processes and mailbox batches.

## How to run it

Use Python 3.11 or newer; development and testing used Python 3.12.13. From the repository root:

```bash
make setup
source .venv/bin/activate
```

Set `ANTHROPIC_API_KEY` in the shell before requesting a report. The Anthropic SDK also respects
`ANTHROPIC_BASE_URL` when supplied by the evaluation environment. Credentials are not loaded from a repository file.
The configured model is `claude-haiku-5-5` with `high` effort, set by `CLAUDE_MODEL` and `CLAUDE_EFFORT` in
`harness/harness.py`; the API account must support that configuration.

```bash
make grade MODE=both
make sheet
make eval
make validate
```

These generate the practice report card, exam report and answer sheet, own-eval results, and submission checks.
`make grade` includes the supplied feedback between practice batches. Fresh extractions make billable API requests.

The direct CLI keeps the assignment's three entry points:

```text
python harness/harness.py ingest <batch_dir> --state-dir <dir>
python harness/harness.py report --as-of YYYY-MM-DD --mode supervised --state-dir <dir>
python harness/harness.py feedback <feedback.json> --state-dir <dir>
```

Reuse the same state directory across batches and feedback. Reports print JSON to stdout; progress and errors go to
stderr. The default mode is supervised. Only documents received by the end of `as_of` in UTC are considered.

All Python dependencies are pinned in `harness/requirements.txt`. Scanned PDF pages use local RapidOCR with bundled
models. On Linux it needs `libgl1`, `libglib2.0-0` and `libxcb1`, which the provided grading container includes.

Additional checks are:

```bash
python -m unittest discover -s tests
python evals/run_evals.py --cases evals/audit_cases \
  --cache-dir out/state_glossary_audit/extractions \
  --out out/glossary_audit_results.json
```

Extraction caches include the source, request, prompt, schema, model and effort in their identity. A repeated report
reuses matching validated results. Add `--cached-only` to the eval runner to block API calls; a missing cache then
fails explicitly. The harness can start with an empty state directory, as the evaluator does.

## What works

The implementation covers all six assignment classes: overcharges, duplicates, authorised waivers, late fees and
payment risk, contractual cashback, and expiry/notice alerts. It reads email bodies, PDF attachments and numbered
Excel statements, with OCR provenance for scanned pages.

Pricing uses pickup-date evidence, weight bands, minimums, contractual units, eligibility and dependent percentage
charges. Monetary calculations use Decimal and half-up rounding; INR conversion happens once per finding using the
invoice-month FX row. Duplicate detection preserves house AWBs and accounts for held credits. Payment calculations
use actual posted movements and keep repeated interest charges within the contractual entitlement.

Signed instruments and category-specific Schedule 1 authority determine which terms count. Unranked binding rate
conflicts include both sources and a monetary range. Supported missing-document cases identify the document and its
holder. Scoped feedback persists, and later batches can revise or retract previous findings with `supersedes`,
`retro` and `learned_at`. Historical reports exclude later arrivals.

Supervised outbound remedies are drafts. Autonomous remedies require all five glossary checks; outbound findings
on one invoice share the inclusive USD 5,000 approval threshold. Source text addressed to an AI has no authority.

Verified results are:

| Check | Result | Record |
|---|---|---|
| Saved live practice run, both modes | 99.71/100; all hard gates clear | `report_card.json`, `report_card.txt` |
| Main eval set: 25 own cases and two samples | 27/27 pass using validated extraction caches | `evals/results.json` |

Additional verification passed a final-code practice replay with API blocked (99.95/100, with all answer-sheet rows
matching), eight glossary audit cases in both fresh-API and offline runs, and all 301 unit and regression tests.
These are completed development checks; their separate local output files are not included in this submission.
The commands above reproduce the unit suite and glossary cases; fresh glossary extraction requires API access.

The exam report passes schema and citation validation. A fresh-state offline replay exactly reproduces the saved
exam report and answer sheet, including USD 8,165.33 total savings. There is no public exam answer key, so this is
reproducibility and evidence validation rather than confirmation of the company's expected answers.

## What doesn't

Fresh model extraction can still misread an unfamiliar layout or clause. Source and schema checks reject unsupported
results and permit one correction; persistent failures stop the report with an error. API errors and large-document
latency can therefore prevent a report within the grader's time limit. OCR can also lose text in poor-quality scans.

Some incomplete or contradictory records remain in `diagnostics`, including unreconciled invoice copies and missing
payment facts. These are not assertions that the invoice is clean; the corresponding money cannot be established.
Missing-source handling is not complete for every possible document combination.

Actions are represented by the prescribed report labels and outbound payloads. There is no external email or payment
transport. Confidence has been checked on our evals, but the successful final runs contain no incorrect reported
items, so AUROC is unmeasurable and confidence on unseen errors remains uncertain. Hidden batches have not been run.

## The eval case that caught a bug

`evals/cases/own_cashback_margin_annual.yaml` caught a percentage-unit error in live extraction. Claude returned
fractions such as `0.20` and `0.10` for clauses printing 20% and 10%, while the calculations expected percentage
points. That generated unexpected per-invoice overcharges and incorrect cashback.

The fix normalises a fraction only when its cited clause explicitly prints the value multiplied by 100 as a
percentage, and the unscaled value is not itself a printed percentage. It operates on a copy of the extraction,
preserving the raw cache, and has regression coverage for both fractions and already-correct percentage points.

The case's independent arithmetic is: three USD 120 invoices cross the USD 200 quarterly threshold on the second
invoice; only the third earns a USD 10 margin reduction. An issued USD 3 credit leaves USD 7 owed. Net annual
qualifying spend is USD 357, so the 2% annual credit is USD 7.14. Both findings passed fresh API testing and subsequent
offline replay.

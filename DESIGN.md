# Design

Claude interprets documents; Python checks evidence, calculates amounts, resolves authority, and selects actions
through the prescribed CLI and report schema.

## Fact model

State lives under `--state-dir` across CLI processes. Each batch retains original emails and `documents.json`:
document ID, UTC receipt timestamp, and numbered pages. IDs use `batch/email.eml#attachment`; bodies have separate IDs.
The supplied reader handles native text; local RapidOCR handles blank PDF pages, retaining recognition scores and boxes.

Structured facts include invoice lines, contractual rates and dates, contacts, shipments, payments, and credits.
Evidence records the document, page, and validated quotation. Keys follow the glossary: invoice/class,
contract/class for expiry, and contract/period/credit type for cashback. Calculations use Decimal, contractual units,
half-up rounding, and one invoice-month FX conversion per finding.

JSON keeps state inspectable. Extraction hashes cover source input, prompt, schema, model, and effort; cache hits are
revalidated. Atomic writes and locks prevent concurrent duplicate extraction requests within a process.

## Conflicts

Candidates must match the contract, lane, charge, and applicable date. Executed amendments and category-specific
Schedule 1 authority determine which changes count. Signed amendments outrank the MSA; later applicable amendments
replace earlier ones. Explicit contractual precedence resolves competing notices and amendments.

Binding, unranked sources giving different correct amounts produce a conflict with both citations and a nonnegative
overpayment range, including dependent fuel. A missing named deciding document produces `cannot_determine`
with its holder. Drafts, unsupported quotations, and unauthorised changes cannot create binding conflicts.
Instructions addressed to an AI are treated as document data.

## Confidence

`evidence-v2` combines evidence completeness, source authority, field extraction support, source agreement, and clause
validity through a weighted logistic score. OCR uses the weakest cited recognition score. Relevant diagnostics reduce
completeness and validity. The report exposes these factors in `confidence_basis`.

A regularised calibration is accepted only when Brier improves with entire mailbox groups held out.
The saved own-eval artifact contains 18 correct items across ten groups; held-out Brier
improved from 0.018784 to 0.011524. With no incorrect labels, AUROC is unmeasurable and error discrimination remains
unproven. Confidence estimates correctness of the verdict and amount; it cannot bypass execution checks.

## People in the loop

Conflicts escalate; expiry alerts and payment reminders remain internal. Supervised outbound remedies are drafts,
with human review at USD 5,000 or more. The inclusive threshold aggregates outbound findings on one invoice or
cashback key.

Autonomous disputes, credit claims, and document requests require five checks: complete deciding evidence,
no overlapping unresolved conflict, an amount below the threshold, reversibility, and sufficient authority including
an approved recipient. Pricing evidence includes reliable pickup support where required. Checks and recipient/payload
are recorded. Irreversible actions require a person. Execution is represented in report actions;
no external transport is implemented.

## Feedback

Feedback persists with its source, decision date, batch, item ID, and hash. Repeated input is idempotent;
changed content under an existing ID is rejected. Reports exclude decisions after `as_of`.

Standing facts apply only when every scope filter and validity date matches. One-off decisions are restricted to
their finding key. Closing an invoice does not close later invoices; accepting a rate for one lane does not affect
another. Later signed authority takes precedence. Changes carry `learned_at` and feedback provenance.

## New documents

Repeated ingestion preserves stable document IDs. Reporting first filters receipt dates, then resolves applicable
terms using contractual date rules. Extraction hashes determine reuse; new layouts use the same schemas and source checks.

Validated report snapshots record source hashes and finding revisions. Changed keys receive `supersedes`; newly
established keys caused by backdated pricing receive `retro`. Proven clean decisions can retract prior findings;
expired alerts and settled payment risks disappear. Historical replay excludes future arrivals. Unsupported
extractions receive one correction attempt, then fail explicitly rather than publishing guessed facts.

## Cost

The configured model is `claude-haiku-5-5`, high effort. Reported billing totals across all Nova API development,
debugging and testing are 18,634,979 input tokens, 5,558,647 output tokens, and USD 4.64 spent: 24,193,626 tokens
combined. This includes repeated runs. A corresponding document count is unavailable, so these totals are not a
measured cost per 1,000 documents.

Planning estimate for 1,000 new documents: assume an average of 20,000 input and 5,000 output tokens per document,
including prompts, schemas, classification and applicable extraction passes. At the
[Anthropic prices checked on 11 October 2026](https://platform.claude.com/docs/en/about-claude/pricing),
Haiku 5.5 costs USD 0.10 per million input tokens and USD 0.50 per million output tokens for requests with at most
100,000 prompt tokens. That gives `(20 million x 0.10) + (5 million x 0.50) = USD 4.50`;
budget about USD 7 with a 50% retry allowance. This is an assumption-based API estimate, not a measured benchmark.
Larger prompts use five times these rates. Matching validated extraction caches incur no new model charges;
local OCR and computation are excluded from the API estimate. Hosting costs are also excluded.

## What I'd do next

Priorities would be persisting actual usage and latency, benchmarking larger mailboxes,
expanding missing-document coverage, and testing extraction across more layouts and degraded scans.
Calibration needs independently labelled failures as well as successes. JSON history would eventually need indexing
and transactional protection across concurrent CLI processes. The main uncertainty is semantic extraction on unseen
clauses: a valid quotation does not prove its interpretation. External delivery would require separate integration
and approval design.

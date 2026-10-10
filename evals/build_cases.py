"""Generate original eval mailboxes and independent expected decisions."""
from __future__ import annotations

import json
import sys
from email import policy
from email.parser import BytesParser
from pathlib import Path

import pymupdf
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from live_mailboxes import MailboxBuilder, build, build_feedback, build_authority  # noqa: E402


def expectation(item):
    if item["verdict"] == "no_finding":
        item = {"action": "none", "amount_usd": None, **item}
    key = {field: item[field] for field in ("class", "invoice_id", "contract_id", "period", "credit_type") if field in item}
    return {"key": key, **{field: value for field, value in item.items() if field not in key}}


def write_case(name, mailbox, as_of, target, checks, why):
    case = {"name": name.replace("_", " "), "mailbox": mailbox.relative_to(ROOT).as_posix(),
            "as_of": as_of, "mode": "supervised", "expect": expectation(target),
            "checks": [expectation(item) for item in checks], "why": why}
    (ROOT / "evals/cases" / f"own_{name}.yaml").write_text(yaml.safe_dump(case, sort_keys=False), encoding="utf-8")


def replace_invoice_layout(root, invoice_id, amount, *, scan):
    """Write a reordered commercial invoice, optionally rasterising away its text layer."""
    path = root / "batch_01/0003.eml"
    message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    text = f"""Bluejay Air Ltd.
INVOICE {invoice_id}
Service agreement: C-71     Currency: USD
Pay by: 2034-06-28         Issued: 2034-01-05
Shipment: BJ-A1             Route: NORTH-SOUTH
Carrier pickup: 2034-01-02

Service                 Weight     Unit price        Extended amount
SEC Security screening  10 kg      USD {amount / 10:.2f}/kg     USD {amount:.2f}

Invoice total: USD {amount:.2f}
"""
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((35, 50), text, fontname="cour", fontsize=10)
        if scan:
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
            with pymupdf.open() as raster:
                raster.new_page(width=page.rect.width, height=page.rect.height).insert_image(page.rect, stream=pixmap.tobytes("png"))
                raw = raster.tobytes()
        else:
            raw = pdf.tobytes()
    attachment = next(message.iter_attachments())
    attachment.set_payload(raw)
    del attachment["Content-Transfer-Encoding"]
    from email.encoders import encode_base64

    encode_base64(attachment)
    path.write_bytes(message.as_bytes())


def build_sections_18_19():
    """Build only the added section fixtures, preserving earlier mailbox sources."""
    folders = ROOT / "evals/mailboxes"
    for name in ("cashback_rebate", "cashback_margin_annual"):
        mailbox = folders / name
        as_of, expected, _ = build(name, mailbox)
        write_case(name, mailbox, as_of, expected[0], expected,
                   "Closed periods use billed qualifying spend and subtract held issued credits for the exact credit key.")
    for name in ("feedback_learning", "feedback_rates", "feedback_authority"):
        mailbox = folders / name
        as_of, expected, _, plan = build_feedback(name, mailbox)
        for key, item_id in plan["learned"]:
            next(item for item in expected if all(item.get(field) == value for field, value in key.items()))["learned_at"] = {
                "feedback_item_id": item_id, "batch": "batch_01"}
        feedback_file = mailbox / "feedback" / "OWN_batch_01.json"
        feedback_file.parent.mkdir(parents=True, exist_ok=True)
        feedback_file.write_text(json.dumps(plan["feedback"], indent=2) + "\n")
        write_case(name, mailbox, as_of, expected[0], expected,
                   "Human decisions persist, carry learned_at, and affect only their explicit key/person/category/lane/month scope.")
        path = ROOT / "evals/cases" / f"own_{name}.yaml"
        case = yaml.safe_load(path.read_text())
        case["feedback"] = {"batch_01": feedback_file.relative_to(ROOT).as_posix()}
        path.write_text(yaml.safe_dump(case, sort_keys=False))


def build_sections_20_21():
    for name in ("authority_precedence", "unranked_conflict"):
        mailbox = ROOT / "evals/mailboxes" / name
        as_of, expected, _, _ = build_authority(name, mailbox)
        write_case(name, mailbox, as_of, expected[0], expected,
                   "Only original binding instruments in force on pickup count; contract precedence ranks them, "
                   "and any unranked alternatives include the correct dependent fuel in their USD range.")


def main():
    folders = ROOT / "evals/mailboxes"
    golden = {}
    for name in ("accessorial_fx", "percentage_minimum", "payment_risk", "compounding_extension",
                 "credit_rebill", "waiver_scope", "missing_fx"):
        mailbox = folders / name
        as_of, expected, probes = build(name, mailbox)
        golden[name] = (mailbox, as_of, expected, probes)
    scenarios = [
        ("accessorial_fx", "accessorial_fx", 0, "Correct kg/shipment/month rates, storage days and eligibility produce INR 20,500 excess; February FX 125 gives USD 164."),
        ("percentage_dependencies", "percentage_minimum", 0, "SEC 30, PCT 3, documented COST 100 and capped MAR 18: excess 31.50. Source injection cannot change these terms."),
        ("compound_fee", "compounding_extension", 0, "Monthly half-up compounding gives 2 + 2.04 + 2.08 = 6.12; billed 9 minus 6.12 = 2.88."),
        ("extended_due_date", "compounding_extension", 1, "The authorised extension precedes payment, so the USD 2 late fee is entirely not owed."),
        ("partial_credit_duplicate", "credit_rebill", 0, "The second USD 20 charge duplicates the first except for its documented USD 5 reversal: recover 15."),
        ("conditional_waiver", "waiver_scope", 0, "BJ-A1's one-off USD 20 waiver applies because the AP register proves payment before its condition deadline."),
        ("missing_fx_document", "missing_fx", 0, "The held Annex F has no April FX rate. Request Annex F from its named treasury contact; do not invent a USD amount."),
    ]
    for name, source, index, why in scenarios:
        mailbox, as_of, expected, _ = golden[source]
        write_case(name, mailbox, as_of, expected[index], expected, why)
    mailbox, _, _, probes = golden["payment_risk"]
    for index, label in ((0, "risk_outside_15_days"), (1, "risk_at_14_days"), (2, "risk_at_1_day"), (3, "risk_on_due_date")):
        as_of, expected = probes[index]
        target = next((item for item in expected if item["class"] == "late_fee_risk"),
                      {"class": "late_fee_risk", "invoice_id": "RISK-100", "verdict": "no_finding"})
        write_case(label, mailbox, as_of, target, expected + ([target] if target not in expected else []),
                   "Only days 1 through 14 trigger reminders; undisputed principal is 105 - 5 = 100, and 2% is USD 2.")
    mailbox, _, _, probes = golden["credit_rebill"]
    target = {"class": "duplicate", "invoice_id": "CREDIT-200", "verdict": "no_finding"}
    write_case("future_rebill_excluded", mailbox, probes[0][0], target, [target],
               "A January report cannot see the April credit or the May rebill even though both batches have been ingested.")
    mailbox, as_of, expected, _ = golden["waiver_scope"]
    target = {"class": "waived_but_billed", "invoice_id": "WAIVER-200", "verdict": "no_finding"}
    write_case("other_shipment_clean", mailbox, as_of, target, expected + [target],
               "The waiver explicitly names only BJ-A1 and WAIVER-100; WAIVER-200 stays clean.")
    for name, scan, minimum, amount in (("scanned_overcharge", True, False, 26),
                                       ("scanned_minimum_clean", True, True, 30),
                                       ("reordered_invoice", False, False, 26)):
        mailbox = folders / name
        box = MailboxBuilder(mailbox)
        box.contract(minimums=minimum)
        box.tracking(["BJ-A1"])
        invoice_id = "OCR-600" if not minimum else "OCR-700"
        box.invoice(invoice_id, "2034-01-05", [("SEC Security screening", "BJ-A1", 10, "kg", amount / 10, amount)])
        replace_invoice_layout(mailbox, invoice_id, amount, scan=scan)
        target = {"class": "overcharge", "invoice_id": invoice_id,
                  "verdict": "no_finding" if minimum else "finding"}
        if not minimum:
            target.update(amount_usd="6.00", action="draft:dispute")
        write_case(name, mailbox, "2034-01-10", target, [target],
                   "The reordered invoice uses USD 2/kg; 10 kg costs 20 (or the explicit 30 minimum). Rasterised cases contain no PDF text layer.")
    mailbox = folders / "expiry_boundary"
    MailboxBuilder(mailbox).contract()
    for as_of, label, verdict in (("2034-11-01", "expiry_at_60_days", "finding"),
                                 ("2034-10-31", "expiry_outside_61_days", "no_finding")):
        target = {"class": "expiry", "contract_id": "C-71", "verdict": verdict}
        if verdict == "finding":
            target.update(deadline="2034-12-31", amount_usd=None, action="internal:alert")
        write_case(label, mailbox, as_of, target, [target], "The expiry alert includes day 60 and excludes day 61.")
    build_sections_18_19()
    build_sections_20_21()
    count = len(list((ROOT / "evals/cases").glob("own_*.yaml")))
    print(json.dumps({"own_cases": count, "mailboxes": len(list(folders.iterdir()))}))


if __name__ == "__main__":
    main()

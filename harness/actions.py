"""The glossary's five unattended-action checks, evaluated without API calls."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

from harness import common, workflow as w

REVERSIBLE = {"dispute", "claim_credit", "request_document"}
FACT_KINDS = {"invoice", "tracking", "pickup_email", "air_waybill", "credit_note", "payment_ledger"}


def _items(report):
    return report["findings"] + [item for group in report["portfolio"].values() for item in group]


def _received(document, as_of):
    value = datetime.fromisoformat(document["received_at"].replace("Z", "+00:00"))
    return value.astimezone(timezone.utc).date().isoformat() <= as_of


def _scope(row, invoice):
    return {"contract_id": w.identifier(invoice["contract_id"]), "lane": w.identifier(row["line"]["lane"]),
            "charge": row["annotation"]["code"] or row["annotation"]["category"],
            "period": (row.get("pickup") or row["annotation"]["service_month"] or invoice["invoice_date"] or "")[:7]}


def _overlaps(scope, conflict):
    if scope["contract_id"] != conflict["contract_id"]:
        return False
    for field in ("lane", "charge", "period"):
        if scope.get(field) and conflict.get(field) and scope[field] != conflict[field]:
            return False
    return True  # Missing scope cannot prove that two keys are disjoint.


def _source_authority(document, item, rows, context, agreement):
    """Distinguish contractual changes from original company records."""
    from harness import feedback

    info = context["metadata"][document["doc"]]
    if info["content_status"] in ("quoted_only", "draft"):
        return False
    contract = context["contracts"].get(item.get("contract_id"))
    instruments = (contract or {}).get("instruments", {})
    signed = instruments.get(document["doc"])
    if signed and signed["binding_state"] == "executed":
        return True
    if info["kind"] in ("contract", "amendment"):
        return False
    # Contract extraction can cite an original transmitting email as proof
    # that the held agreement was executed. That confirmation is a fact from
    # the named carrier contact, not an independent replacement of its terms.
    execution_cover = any(name.partition("#")[0] == document["doc"]
                          and value["binding_state"] == "executed"
                          and any(ref["doc"] == document["doc"] for ref in value["evidence"])
                          for name, value in instruments.items())
    if execution_cover and w.authorized_contacts(document, agreement["terms"], context["state_dir"], "notices"):
        return True
    facts = next((entry["facts"] for entry in context["records"]
                  if entry["document"]["doc"] == document["doc"]), {})
    category = ("rates" if info["kind"] == "rate_notice" else
                "waivers" if info["kind"] == "waiver" or facts.get("waivers") else
                "payment_terms" if facts.get("extensions") or "payment_terms" in info["changes"] else None)
    if category:
        if info["content_status"] != "original":
            return False
        if w.authorized_contacts(document, agreement["terms"], context["state_dir"], category):
            return True
        for row in rows:
            if (category == "rates" and row.get("accepted_pricing_doc") == document["doc"]
                    and (row.get("feedback_fact") or {}).get("fact", {}).get("type") in ("rate_accepted", "precedence")):
                return True  # The scoped human choice was checked by pricing resolution.
            values = {**item, "lane": row["line"]["lane"], "awb": row["annotation"]["shipment_id"]}
            if feedback.authority(document, agreement["terms"], context, category, values,
                                  document["received_at"][:10]):
                return True
        return False
    records = (info["kind"] in FACT_KINDS or any(facts.get(field) for field in
               ("shipments", "payments", "credits")) or facts.get("mail_intent") == "payment_request")
    # The classifier has no declaration/customs kind. An original attached
    # company record with explicit extracted facts is still level 3, even
    # when that broad classifier returned other/unknown.
    original_record = info["content_status"] == "original" or (
        "#" in document["doc"] and any(facts.get(field) for field in ("shipments", "payments", "credits")))
    return records and original_record and w.company_sender(document, agreement["terms"], context["state_dir"])


def attach(items, context):
    """Keep the facts needed by the final action policy after billing finishes."""
    from harness import billing as b
    from harness.confidence import invoice_field_support

    available = {doc["doc"]: doc for doc in context["documents"]}
    # A citation can be repeated across many rows. Checking the same original
    # source for every quote must not reparse a large MIME attachment each time.
    source_dates = {key: _received(doc, context["as_of"]) for key, doc in available.items()}
    carrier_records = {}
    pickup_rows = defaultdict(list)
    for record in context["invoices"]:
        for row in record["rows"]:
            if row.get("pickup") and row.get("pickup_source") in ("tracking", "pickup_email"):
                pickup_rows[(w.identifier(record["invoice"]["contract_id"]),
                             w.identifier(row["annotation"]["shipment_id"]))].append(row)
    for item in items:
        if item["class"] in ("expiry", "late_fee_risk") or item["verdict"] == "no_finding":
            continue
        remedy = "request_document" if item["verdict"] == "cannot_determine" else (
            "claim_credit" if item["class"] == "cashback" else "dispute")
        records = [record for record in context["invoices"]
                   if (w.identifier(record["invoice"]["invoice_id"]) == item.get("invoice_id")
                       if item.get("invoice_id") else w.identifier(record["invoice"]["contract_id"]) == item.get("contract_id")
                       and any(ref["doc"] == record["document"]["doc"] for ref in item["evidence"]))]
        complete = bool(records)
        authorized = True
        scopes, recipients, recipient_refs = [], {}, []
        source_authorities = {}
        for record in records:
            invoice = record["invoice"]
            indexes = context.get("disputes", {}).get(item.get("invoice_id"), {}).get(item["class"])
            rows = [row for row in record["rows"] if indexes is None or row["index"] in indexes]
            if item["class"] == "cashback":
                rows = [row for row in rows if row["annotation"]["category"] not in ("tax", "duty", "late_fee")
                        and (row["line"]["billed_amount"] is None or common.decimal_value(
                            row["line"]["billed_amount"], nonnegative=False) > 0)]
            if item["class"] == "late_fee_not_owed":
                rows = [row for row in rows if row["annotation"]["category"] == "late_fee"]
                expanded = []
                for row in rows:
                    reference = row["annotation"]["reference_invoice_id"]
                    shipments = {(w.identifier(entry["annotation"]["shipment_id"]), w.identifier(entry["line"]["lane"]))
                                 for other in context["invoices"] for entry in other["rows"]
                                 if w.identifier(other["invoice"]["invoice_id"]) == w.identifier(reference)
                                 and w.identifier(other["invoice"]["contract_id"]) == w.identifier(invoice["contract_id"])
                                 and entry["annotation"]["shipment_id"]}
                    expanded += [{**row, "_action_shipment_id": shipment, "_action_lane": lane}
                                 for shipment, lane in sorted(shipments)] if shipments else [row]
                rows = expanded
            if not rows:
                complete = False
            for row in rows:
                on_date = row.get("pickup")
                awb = w.identifier(row.get("_action_shipment_id") or row["annotation"]["shipment_id"] or row["line"]["awb"])
                if not awb and row["annotation"]["reference_invoice_id"]:
                    referenced = [entry for other in context["invoices"] for entry in other["rows"]
                                  if w.identifier(other["invoice"]["invoice_id"]) == w.identifier(row["annotation"]["reference_invoice_id"])]
                    shipments = {w.identifier(entry["annotation"]["shipment_id"]) for entry in referenced
                                 if entry["annotation"]["shipment_id"]}
                    if len(shipments) == 1:
                        awb = next(iter(shipments))
                if awb:
                    candidates = pickup_rows[(w.identifier(invoice["contract_id"]), awb)]
                    dates = {entry["pickup"] for entry in candidates}
                    if len(dates) != 1:
                        complete = False
                    else:
                        on_date = next(iter(dates))
                        # The pickup source must itself be an original carrier record.
                        refs = [ref for entry in candidates for ref in entry["evidence"]
                                if context["metadata"][ref["doc"]]["kind"] in ("tracking", "pickup_email")]
                        for ref in refs:
                            cache_key = (record["document"]["doc"], ref["doc"])
                            if cache_key not in carrier_records:
                                carrier_records[cache_key] = w.company_sender(available[ref["doc"]], record["terms"],
                                                                             context["state_dir"], parties=("carrier",))
                        if not refs or any(not carrier_records[record["document"]["doc"], ref["doc"]] for ref in refs):
                            complete = False
                        item["evidence"] += refs
                else:
                    on_date = row["annotation"]["service_month"]
                    on_date = on_date + "-01" if on_date else None
                    # A recurring monthly service has no shipment pickup, but
                    # its stated service period still needs an operative clause.
                    if not on_date:
                        complete = False
                if invoice_field_support({"line_items": [row["line"]]}) < 1:
                    complete = False
                try:
                    agreement = b._agreement(record["contract"], on_date)
                except (ValueError, TypeError, AttributeError):
                    complete = False
                    authorized = False
                    continue
                end = agreement["terms"]["expiry_date"]
                if end and on_date > end and not row.get("continuing_clause"):
                    renewed = agreement["terms"]["auto_renews"] is True
                    notices = (record["contract"] or {}).get("notices", [])
                    if any((notice["notice_for_expiry_date"] or end) < on_date for notice in notices):
                        renewed = False
                    # A signed term extension, already accepted by contract
                    # resolution, can also keep this shipment in term.
                    extended = record["contract"]["terms"].get("expiry_date")
                    extension_refs = record["contract"].get("evidence", [])
                    extension_active = any(context["metadata"][ref["doc"]]["kind"] == "amendment"
                                           and context["metadata"][ref["doc"]]["effective_date"]
                                           and context["metadata"][ref["doc"]]["effective_date"] <= on_date
                                           for ref in extension_refs)
                    if not renewed and not (extension_active and extended and on_date <= extended):
                        complete = False
                if remedy != "request_document" and item["class"] == "overcharge" and row.get("correct") is None:
                    complete = False
                scopes.append({**_scope(row, invoice), "lane": w.identifier(row.get("_action_lane", row["line"]["lane"])),
                               "period": on_date[:7]})
                category = "billing_disputes"
                holder = item.get("missing_doc", {}).get("holder", {}) if item.get("missing_doc") else {}
                if remedy == "request_document":
                    category = holder.get("role")
                contacts = [contact for contact in agreement["terms"]["contacts"]
                            if contact["party"] == "carrier" and contact["category"] == category
                            and (not holder or contact["person"] == holder.get("person"))]
                for contact in contacts:
                    recipients[contact["email"].casefold()] = {
                        "email": contact["email"], "person": contact["person"], "category": contact["category"]}
                    recipient_refs += contact["evidence"]
                for doc_id in {reference["doc"] for reference in item["evidence"]}:
                    source = available.get(doc_id)
                    if source is None or not source_dates[doc_id]:
                        complete = False
                    else:
                        cache_key = (agreement["document"]["doc"], doc_id)
                        if cache_key not in source_authorities:
                            source_authorities[cache_key] = _source_authority(source, item, rows, context, agreement)
                        if not source_authorities[cache_key]:
                            authorized = False
        item["evidence"] = w._dedupe_evidence(item["evidence"] + recipient_refs)
        if remedy == "request_document":
            name = (item.get("missing_doc") or {}).get("name", "")
            complete = complete and bool(name) and any(
                " ".join(name.split()).casefold() in " ".join(ref["quote"].split()).casefold()
                for ref in item["evidence"])
        recipient = next(iter(recipients.values())) if len(recipients) == 1 else None
        item["_execution"] = {"remedy": remedy, "evidence_complete": complete,
                              "source_authority": authorized and bool(recipient),
                              "scope": scopes, "recipient": recipient}


def apply(report):
    """Select report actions only; no email, payment or other external side effects."""
    items = _items(report)
    totals = defaultdict(lambda: Decimal(0))
    conflicts = []
    for item in items:
        if item["verdict"] == "conflict":
            scope = item.get("conflict_scope", {})
            for lane in scope.get("lanes") or [""]:
                for charge in scope.get("codes") or [""]:
                    for period in {value[:7] for value in scope.get("pickup_dates", [])} or {""}:
                        conflicts.append({"contract_id": item.get("contract_id", ""), "lane": w.identifier(lane),
                                          "charge": charge, "period": period})
        if item["verdict"] == "finding" and item["class"] not in ("expiry", "late_fee_risk"):
            key = ("invoice", item["invoice_id"]) if item.get("invoice_id") else (
                "cashback", item.get("contract_id"), item.get("period"), item.get("credit_type"))
            totals[key] += common.decimal_value(item["amount_usd"])
    for item in items:
        proof = item.pop("_execution", {})
        if item["verdict"] == "no_finding":
            item["action"] = "none"
            continue
        if item["verdict"] == "conflict":
            item["action"] = "escalate"
            continue
        if item["class"] in ("expiry", "late_fee_risk"):
            item["action"] = "internal:alert" if item["class"] == "expiry" else "internal:payment_reminder"
            continue
        if item["class"] == "cashback" and item["action"] == "internal:track_credit":
            continue
        remedy = proof.get("remedy", "request_document" if item["verdict"] == "cannot_determine" else
                           "claim_credit" if item["class"] == "cashback" else "dispute")
        key = ("invoice", item["invoice_id"]) if item.get("invoice_id") else (
            "cashback", item.get("contract_id"), item.get("period"), item.get("credit_type"))
        # A document request makes no monetary claim. Its deciding evidence
        # is the held reference naming the absent document and its holder;
        # absence blocks a dispute/credit claim, not the request to obtain it.
        document_request = remedy == "request_document" and item["verdict"] == "cannot_determine"
        checks = {"evidence_complete": bool(proof.get("evidence_complete")) and (document_request or not item.get("missing_doc")),
                  "no_conflict": bool(proof.get("scope")) and not any(
                      _overlaps(scope, conflict) for scope in proof.get("scope", []) for conflict in conflicts),
                  "under_threshold": (item["verdict"] == "finding" or document_request) and totals[key] < Decimal("5000"),
                  "reversible": remedy in REVERSIBLE,
                  "enough_authority": bool(proof.get("source_authority")) and bool(proof.get("recipient"))}
        if remedy not in REVERSIBLE:
            item["action"] = "human_review"
        elif report["mode"] == "supervised":
            item["action"] = "human_review" if not checks["under_threshold"] and item["verdict"] == "finding" else f"draft:{remedy}"
        else:
            item["action"] = f"execute:{remedy}" if all(checks.values()) else "human_review"
        item["action_basis"] = {"checks": checks, "failed_checks": [name for name, passed in checks.items() if not passed],
                                "scope": proof.get("scope", []),
                                "aggregate_outbound_usd": f"{totals[key]:.2f}" if item["verdict"] == "finding" else None}
        subject = (f"Request: {item['missing_doc']['name']}" if remedy == "request_document" else
                   f"Credit review: {item.get('invoice_id') or item.get('contract_id')}")
        body = (f"Please provide {item['missing_doc']['name']}." if remedy == "request_document" else
                f"Please review the {item['class']} finding for {item.get('invoice_id') or item.get('contract_id')}: "
                f"USD {item.get('amount_usd')}, based on the attached source references.")
        item["outbound"] = {"kind": remedy, "recipient": proof.get("recipient"), "subject": subject, "body": body,
                            "evidence": item["evidence"]}

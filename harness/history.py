"""Immutable report snapshots, item revisions and historical-cutoff isolation."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from harness import common, feedback, workflow as w


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _items(report):
    return report["findings"] + [item for group in report["portfolio"].values() for item in group]


def _state(item):
    return {field: item.get(field) for field in ("verdict", "amount_usd", "amount_range", "deadline", "action", "missing_doc", "learned_at")}


def remember_clear(context, item, fact=None):
    """Keep a proven clean decision privately until a prior item needs retracting."""
    cleared = {field: copy.deepcopy(value) for field, value in item.items()
               if field not in ("comparison", "calculation", "amount_native", "conflict_scope", "retro", "feedback_basis")}
    cleared.update(verdict="no_finding", action="none", amount_usd=None, amount_range=None,
                   missing_doc=None, learned_at=None)
    if fact:
        feedback.learned(cleared, fact)
    context.setdefault("cleared", {})[feedback.finding_key(cleared)] = cleared


def _retro(item, documents, metadata):
    on_dates = item.get("pricing_dates") or ([item["invoice_date"]] if item.get("invoice_date") else [])
    for ref in item["evidence"]:
        doc, info = documents[ref["doc"]], metadata[ref["doc"]]
        if info["kind"] not in ("amendment", "rate_notice") or "pricing" not in info["changes"]:
            continue
        effective = info["effective_date"]
        if effective and any(effective <= on_date < doc["received_at"][:10] for on_date in on_dates):
            return True
    return False


def publish(report, documents, metadata, facts, state_dir, contracts, validate, cleared=None):
    """Persist only validated reports; an identical request returns its exact snapshot."""
    path = Path(state_dir) / "report_history.json"
    memory = json.loads(path.read_text()) if path.exists() else {"schema_version": "1.0", "snapshots": []}
    if memory.get("schema_version") != "1.0":
        raise ValueError("Unsupported report history version.")
    for snapshot in memory["snapshots"]:
        if _hash(snapshot["report"]) != snapshot["report_sha256"]:
            raise ValueError("Saved report snapshot failed its integrity check.")
    available = {doc["doc"]: doc for doc in documents}
    sources = {key: _hash(doc) for key, doc in available.items()}
    request = _hash({"sources": sources, "feedback": facts, "proposed_report": report,
                     "cleared": sorted((list(key), value) for key, value in (cleared or {}).items())})
    for snapshot in memory["snapshots"]:
        if snapshot["request_sha256"] == request:
            saved = copy.deepcopy(snapshot["report"])
            validate(saved)
            return saved
    previous = [snapshot for snapshot in memory["snapshots"]
                if snapshot["report"]["mode"] == report["mode"] and snapshot["report"]["as_of"] <= report["as_of"]
                and all(sources.get(key) == value for key, value in snapshot["sources"].items())]
    predecessor = max(enumerate(previous), key=lambda pair: (pair[1]["report"]["as_of"], pair[0]))[1] if previous else None
    old = {feedback.finding_key(item): item for item in _items(predecessor["report"])} if predecessor else {}
    result = copy.deepcopy(report)
    actual = {feedback.finding_key(item): item for item in _items(result)}
    if predecessor:
        for key, prior in old.items():
            if key in actual or prior["class"] in ("expiry", "late_fee_risk") or prior["verdict"] == "no_finding":
                continue
            clean = (cleared or {}).get(key)
            if clean is None:
                continue
            clean = copy.deepcopy(clean)
            from harness.confidence import assess
            assess(clean, available, metadata, contracts, result.get("diagnostics", []))
            if prior["class"] == "cashback":
                result["portfolio"]["cashback"].append(clean)
            else:
                result["findings"].append(clean)
    for item in _items(result):
        key = feedback.finding_key(item)
        prior = old.get(key)
        if prior and _state(prior) == _state(item):
            item["id"], item["supersedes"] = prior["id"], list(prior["supersedes"])
            if prior.get("retro"):
                item["retro"] = True
        else:
            base = item["id"].split(":v:")[0]
            item["id"] = f"{base}:v:{_hash([key, _state(item), prior['id'] if prior else request])[:16]}"
            item["supersedes"] = [prior["id"]] if prior else []
            if not prior and _retro(item, available, metadata):
                item["retro"] = True
    result["findings"].sort(key=lambda item: item["id"])
    for group in result["portfolio"].values():
        group.sort(key=lambda item: item["id"])
    validate(result)
    memory["snapshots"].append({"request_sha256": request, "sources": sources,
                                "report_sha256": _hash(result), "report": result})
    common.write_json(path, memory)
    return result

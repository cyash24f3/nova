"""Evidence-based probability estimates and evaluation-only calibration metrics."""
from __future__ import annotations

import math
import re
import json
import calendar
from datetime import date
from functools import lru_cache
from pathlib import Path

MODEL_VERSION = "evidence-v2"
FACTOR_WEIGHTS = {"evidence_completeness": 1.0, "source_authority": 1.1,
                  "field_extraction": 1.0, "source_agreement": .8, "clause_in_force": .8}
CALIBRATION_PATH = Path(__file__).with_name("confidence_calibration.json")


def _sigmoid(value):
    return 1 / (1 + math.exp(-max(-40, min(40, value))))


def _logit(probability):
    if not math.isfinite(probability) or not 0 < probability < 1:
        raise ValueError("Calibration needs probabilities strictly between zero and one.")
    return math.log(probability / (1 - probability))


def raw_probability(factors):
    if set(factors) != set(FACTOR_WEIGHTS) or any(
            not math.isfinite(value) or not 0 <= value <= 1 for value in factors.values()):
        raise ValueError("Invalid evidence confidence factors.")
    value = -1.5 + sum(FACTOR_WEIGHTS[key] * (2 * score - 1) for key, score in factors.items())
    return min(.97, max(.05, _sigmoid(value)))


@lru_cache(maxsize=1)
def load_calibration():
    if not CALIBRATION_PATH.is_file():
        return None
    model = json.loads(CALIBRATION_PATH.read_text())
    if (model.get("schema_version") != "1.0" or model.get("feature_model") != MODEL_VERSION
            or not isinstance(model.get("accepted"), bool)):
        raise ValueError("Unsupported confidence calibration artifact.")
    for key, lower, upper in (("scale", .25, 4), ("bias", -2, 2)):
        value = model.get(key)
        if not isinstance(value, (int, float)) or not math.isfinite(value) or not lower <= value <= upper:
            raise ValueError("Invalid confidence calibration parameter.")
    return model


def calibrated_probability(probability, calibration):
    if calibration is None or not calibration["accepted"]:
        return probability
    return min(.97, max(.05, _sigmoid(calibration["scale"] * _logit(probability) + calibration["bias"])))


def _fit(observations, regularization):
    scale, bias = 1., 0.
    counts = [sum(row["correct"] is label for row in observations) for label in (True, False)]
    fit_scale = min(counts) >= 5
    for _ in range(100):
        gs, gb = regularization * (scale - 1), regularization * bias
        hss = hbb = regularization
        hsb = 0.
        for row in observations:
            x = _logit(row["raw_probability"])
            p = _sigmoid(scale * x + bias)
            residual, variance = p - int(row["correct"]), p * (1 - p)
            gs += residual * x
            gb += residual
            hss += variance * x * x
            hsb += variance * x
            hbb += variance
        if fit_scale:
            determinant = hss * hbb - hsb * hsb
            ds, db = (hbb * gs - hsb * gb) / determinant, (hss * gb - hsb * gs) / determinant
        else:
            ds, db = 0., gb / hbb
        new_scale, new_bias = min(4., max(.25, scale - ds)), min(2., max(-2., bias - db))
        if abs(new_scale - scale) + abs(new_bias - bias) < 1e-9:
            break
        scale, bias = new_scale, new_bias
    return {"scale": scale, "bias": bias, "accepted": True,
            "method": "regularized_platt" if fit_scale else "regularized_intercept"}


def fit_calibration(observations, *, regularization=4.):
    """Hold out whole mailbox groups; deploy only when held-out Brier improves."""
    if not math.isfinite(regularization) or regularization <= 0:
        raise ValueError("Calibration regularization must be positive.")
    for row in observations:
        _logit(row["raw_probability"])
        if not isinstance(row["correct"], bool) or not isinstance(row["group"], str) or not row["group"]:
            raise ValueError("Calibration needs boolean labels and mailbox groups.")
    groups = sorted({row["group"] for row in observations})
    if len(observations) < 12 or len(groups) < 4:
        raise ValueError("Calibration needs at least 12 scored items from four mailbox groups.")
    folds = [set(groups[index::5]) for index in range(min(5, len(groups)))]
    held_out, baseline, splits = [], [], []
    for groups_out in folds:
        train = [row for row in observations if row["group"] not in groups_out]
        test = [row for row in observations if row["group"] in groups_out]
        model = _fit(train, regularization)
        held_out += [(calibrated_probability(row["raw_probability"], model), row["correct"]) for row in test]
        baseline += [(row["raw_probability"], row["correct"]) for row in test]
        splits.append({"training_groups": len(groups) - len(groups_out), "held_out_groups": len(groups_out),
                       "training_items": len(train), "held_out_items": len(test)})
    before, after = metrics(baseline), metrics(held_out)
    model = _fit(observations, regularization)
    model.update(schema_version="1.0", feature_model=MODEL_VERSION,
                 accepted=after["brier"] < before["brier"], regularization=regularization,
                 observations=len(observations), groups=len(groups),
                 correct=sum(row["correct"] for row in observations),
                 incorrect=sum(not row["correct"] for row in observations),
                 validation={"method": "grouped_cross_validation", "folds": splits,
                             "baseline": before, "calibrated": after})
    return model


def _normal(text):
    return "".join(str(text).split()).casefold()


def _date_support(value, quote):
    """Verify a normalised ISO date in common unambiguous printed formats."""
    try:
        day = date.fromisoformat(value)
    except ValueError:
        return False
    variants = {value, f"{day.year}/{day.month:02}/{day.day:02}"}
    for month in (calendar.month_abbr[day.month], calendar.month_name[day.month]):
        for number in (str(day.day), f"{day.day:02}"):
            variants.update((f"{number} {month} {day.year}", f"{number}-{month}-{day.year}",
                             f"{month} {number}, {day.year}", f"{month} {number} {day.year}"))
    # Whitespace is immaterial, but a date must not match a substring of a
    # longer number, such as 2 Aug matching the tail of 22 Aug.
    return any(re.search(r"(?<!\d)" + re.escape(text.casefold()).replace(r"\ ", r"\s*") + r"(?!\d)", quote.casefold())
               for text in variants)


def invoice_field_support(invoice: dict) -> float:
    """Check each printed row field against its own exact source quote."""
    checks = []
    for line in invoice.get("line_items", []):
        quote = _normal(line["source_quote"]).replace(",", "")
        for field in ("awb", "pickup_date", "lane", "quantity", "rate", "billed_amount"):
            value = line.get(field)
            if value is None:
                continue
            # Boundaries prevent a rate of 2 being supported only by an amount of 25.
            token = _normal(value).replace(",", "").rstrip("%")
            if field in ("quantity", "rate", "billed_amount"):
                matched = re.search(r"(?<![\d.])" + re.escape(token) + r"(?![\d.])", line["source_quote"].replace(",", ""))
            elif field == "pickup_date":
                matched = _date_support(value, line["source_quote"])
            else:
                matched = token in quote
            checks.append(bool(matched))
    return sum(checks) / len(checks) if checks else 0.5


def _recognition_quality(document, reference):
    index = reference["page"] - 1
    sources = document.get("page_sources", [])
    if index >= len(sources) or sources[index]["method"] == "native":
        return 1.0
    source = sources[index]
    quote = _normal(reference["quote"])
    scores = [token["score"] for token in source.get("tokens", [])
              if _normal(token["text"]) in quote]
    # Use the weakest cited box, so a low-quality monetary field isn't hidden by a long header.
    return min(scores, default=source.get("min_score", 0))


def assess(item, documents, metadata, contracts, diagnostics):
    refs = {(ref["doc"], ref["page"], ref["quote"]): ref for ref in item["evidence"]}.values()
    refs = list(refs)
    held = [ref for ref in refs if ref["doc"] in documents]
    completeness = len(held) / len(refs) if refs else 0.0
    contract = contracts.get(item.get("contract_id"))
    agreements = contract.get("agreements", [contract]) if contract else []
    signed = {record["document"]["doc"] for record in agreements
              if record["terms"].get("binding_state") == "executed"}
    has_signed_evidence = any(ref["doc"] in signed for ref in held)
    authority = 1.0 if has_signed_evidence else 0.35
    statuses = [metadata.get(ref["doc"], {}).get("content_status", "unknown") for ref in held]
    if any(status in ("quoted_only", "draft") for status in statuses):
        authority *= 0.25
    elif "unknown" in statuses:
        authority *= 0.8
    recognition = min((_recognition_quality(documents[ref["doc"]], ref) for ref in held), default=0)
    inputs = item.pop("_confidence_inputs", {})
    extraction = recognition * inputs.get("field_support", 0.85)
    invoice_sources = {ref["doc"] for ref in held if metadata.get(ref["doc"], {}).get("kind") == "invoice"}
    kinds = {metadata.get(ref["doc"], {}).get("kind") for ref in held}
    independent = (item["class"] == "duplicate" and len(invoice_sources) >= 2
                   or item["class"] in ("late_fee_risk", "late_fee_not_owed") and "payment_ledger" in kinds)
    agreement = inputs.get("source_agreement", 1.0 if independent else 0.5)
    validity = inputs.get("clause_in_force", 0.5)
    relevant = [diagnostic for diagnostic in diagnostics
                if item.get("invoice_id") and diagnostic.get("invoice_id") == item["invoice_id"]]
    if relevant:
        completeness *= 0.7
        validity *= 0.7
    if item["verdict"] == "cannot_determine":
        # Confidence is in the abstention, not in an invented monetary amount.
        missing = item.get("missing_doc") or {}
        completeness *= 1.0 if missing.get("name") and missing.get("holder") else 0.4
    factors = {"evidence_completeness": completeness, "source_authority": authority,
               "field_extraction": extraction, "source_agreement": agreement,
               "clause_in_force": validity}
    probability = raw_probability(factors)
    calibration = load_calibration()
    item["confidence"] = round(calibrated_probability(probability, calibration), 4)
    item["confidence_basis"] = {"model": MODEL_VERSION, "raw_probability": round(probability, 6),
                                "calibrated": bool(calibration and calibration["accepted"]),
                                "factors": {key: round(value, 4) for key, value in factors.items()}}


def metrics(observations: list[tuple[float, bool]]) -> dict:
    """Brier, AUROC with ties, and reliability bins; never read by production scoring."""
    if any(not math.isfinite(p) or not 0 <= p <= 1 for p, _ in observations):
        raise ValueError("Confidence probabilities must be finite and between 0 and 1.")
    positives = [p for p, correct in observations if correct]
    negatives = [p for p, correct in observations if not correct]
    auc = (sum((a > b) + .5 * (a == b) for a in positives for b in negatives)
           / (len(positives) * len(negatives))) if positives and negatives else None
    bins = []
    for index in range(10):
        values = [(p, correct) for p, correct in observations if min(int(p * 10), 9) == index]
        if values:
            bins.append({"lower": index / 10, "upper": (index + 1) / 10, "count": len(values),
                         "mean_confidence": round(sum(p for p, _ in values) / len(values), 6),
                         "accuracy": round(sum(correct for _, correct in values) / len(values), 6)})
    return {"model": MODEL_VERSION, "items_scored": len(observations),
            "correct": len(positives), "incorrect": len(negatives),
            "brier": round(sum((p - int(correct)) ** 2 for p, correct in observations) / len(observations), 6) if observations else None,
            "auroc": round(auc, 6) if auc is not None else None,
            "reliability_bins": bins,
            "auroc_note": None if auc is not None else "Requires both correct and incorrect reported items."}

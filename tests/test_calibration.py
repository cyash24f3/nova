"""Empirical fitting, mailbox-held-out validation and conservative fallbacks."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import confidence


class CalibrationTests(unittest.TestCase):
    def observations(self, single_class=False):
        return [{"group": f"mailbox-{group}", "raw_probability": .8 if label else .2, "correct": label}
                for group in range(6) for label in ([True] * 4 if single_class else [True, False] * 2)]

    def test_grouped_fit_improves_brier_and_never_reverses_evidence_order(self):
        model = confidence.fit_calibration(self.observations())
        self.assertTrue(model["accepted"])
        self.assertEqual(model["method"], "regularized_platt")
        self.assertLess(model["validation"]["calibrated"]["brier"], model["validation"]["baseline"]["brier"])
        self.assertEqual(sum(fold["held_out_items"] for fold in model["validation"]["folds"]), 24)
        self.assertTrue(all(fold["training_groups"] + fold["held_out_groups"] == 6 for fold in model["validation"]["folds"]))
        scores = [confidence.calibrated_probability(p, model) for p in (.1, .3, .5, .7, .9)]
        self.assertEqual(scores, sorted(scores))

    def test_single_label_data_fits_only_intercept_and_does_not_invent_auc(self):
        model = confidence.fit_calibration(self.observations(single_class=True))
        self.assertEqual(model["method"], "regularized_intercept")
        self.assertEqual(model["scale"], 1)
        self.assertEqual(model["incorrect"], 0)
        self.assertIsNone(model["validation"]["calibrated"]["auroc"])
        self.assertTrue(model["accepted"])

    def test_insufficient_nonfinite_or_unlabelled_training_is_rejected(self):
        invalid = [self.observations()[:3]]
        for field, value in (("raw_probability", float("nan")), ("raw_probability", 1),
                             ("correct", "true"), ("group", "")):
            rows = self.observations()
            rows[0][field] = value
            invalid.append(rows)
        for rows in invalid:
            with self.subTest(rows=rows[:1]), self.assertRaises(ValueError):
                confidence.fit_calibration(rows)

    def test_absent_or_rejected_artifact_keeps_raw_probability(self):
        self.assertEqual(confidence.calibrated_probability(.8, None), .8)
        self.assertEqual(confidence.calibrated_probability(.8, {"accepted": False}), .8)

    def test_loaded_artifact_is_versioned_validated_and_has_no_answer_identifiers(self):
        artifact = confidence.load_calibration()
        self.assertEqual(artifact["feature_model"], confidence.MODEL_VERSION)
        self.assertEqual(artifact["provenance"]["api_calls"], 0)
        self.assertNotIn("invoice_id", json.dumps(artifact))
        self.assertNotIn("data/scenarios/answers", json.dumps(artifact))

    def test_corrupt_or_nonmonotone_artifact_is_rejected(self):
        original = confidence.load_calibration()
        for field, value in (("scale", -1), ("bias", float("inf")), ("feature_model", "obsolete")):
            model = copy.deepcopy(original)
            model[field] = value
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "calibration.json"
                path.write_text(json.dumps(model))
                confidence.load_calibration.cache_clear()
                try:
                    with patch.object(confidence, "CALIBRATION_PATH", path), self.assertRaises(ValueError):
                        confidence.load_calibration()
                finally:
                    confidence.load_calibration.cache_clear()

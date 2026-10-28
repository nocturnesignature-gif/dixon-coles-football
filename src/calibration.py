"""Per-outcome calibration for Dixon-Coles 1X2 probabilities."""

import math

HIGH_PROBABILITY_THRESHOLD = 0.65
OBSERVED_HIGH_PROBABILITY_HIT_RATE = 0.6412451361867705
MIN_HISTORY_FOR_FITTING = 50
L2_PENALTY = 0.5
LEARNING_RATE = 0.05
EPOCHS = 300


def _sigmoid(x):
    return 1 / (1 + math.exp(-max(-30, min(30, x))))


def _fit_logistic(samples, initial_weight=0.0, initial_bias=0.0):
    weight, bias = initial_weight, initial_bias
    n = len(samples)
    if not n:
        return weight, bias
    for _ in range(EPOCHS):
        weight_gradient = 0.0
        bias_gradient = 0.0
        for probability, label in samples:
            prediction = _sigmoid(weight * probability + bias)
            error = prediction - label
            weight_gradient += error * probability
            bias_gradient += error
        weight -= LEARNING_RATE * (weight_gradient / n + L2_PENALTY * weight / n)
        bias -= LEARNING_RATE * (bias_gradient / n)
    return weight, bias


def calibrated_probability(probability):
    probability = float(probability)
    if probability <= HIGH_PROBABILITY_THRESHOLD:
        return probability
    excess = probability - HIGH_PROBABILITY_THRESHOLD
    target_excess = OBSERVED_HIGH_PROBABILITY_HIT_RATE - HIGH_PROBABILITY_THRESHOLD
    shrinkage = target_excess / (1 - HIGH_PROBABILITY_THRESHOLD)
    return HIGH_PROBABILITY_THRESHOLD + excess * shrinkage


def _legacy_calibrate(home, draw, away):
    raw = {
        "home": calibrated_probability(home),
        "draw": calibrated_probability(draw),
        "away": calibrated_probability(away),
    }
    total = sum(raw.values())
    return {key: value / total for key, value in raw.items()}


def calibrate_1x2(home, draw, away, history=None):
    """Calibrate 1X2 probabilities using per-outcome historical calibration.

    history is an iterable of dictionaries with home_probability, draw_probability,
    away_probability and actual_outcome. If fewer than MIN_HISTORY_FOR_FITTING
    valid rows are supplied, fall back to the legacy high-probability shrinkage.
    """
    home, draw, away = float(home), float(draw), float(away)
    rows = []
    if history:
        for row in history:
            outcome = row.get("actual_outcome")
            values = {
                "home": row.get("home_probability"),
                "draw": row.get("draw_probability"),
                "away": row.get("away_probability"),
            }
            if outcome not in values or any(values[k] is None for k in values):
                continue
            rows.append((
                {k: max(min(float(v), 0.999), 0.001) for k, v in values.items()},
                outcome,
            ))

    if len(rows) < MIN_HISTORY_FOR_FITTING:
        return _legacy_calibrate(home, draw, away)

    calibrated = {}
    models = {}
    for outcome in ("home", "draw", "away"):
        samples = [(values[outcome], 1 if actual == outcome else 0) for values, actual in rows]
        weight, bias = _fit_logistic(samples)
        models[outcome] = (weight, bias)
        calibrated[outcome] = _sigmoid(weight * max(min(home if outcome == "home" else draw if outcome == "draw" else away, 0.999), 0.001) + bias)

    total = sum(calibrated.values())
    if total <= 0:
        return _legacy_calibrate(home, draw, away)
    return {key: value / total for key, value in calibrated.items()}

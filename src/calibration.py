"""Calibration utilities for Dixon-Coles probabilities.

The backtest found that predictions above 65% were overconfident: the model
predicted 76.3% on average but observed 64.1%. This module shrinks
high-probability outcomes toward the observed hit rate, then renormalizes the
1X2 distribution.
"""

HIGH_PROBABILITY_THRESHOLD = 0.65
OBSERVED_HIGH_PROBABILITY_HIT_RATE = 0.6412451361867705


def calibrated_probability(probability: float) -> float:
    """Shrink probabilities above the threshold toward observed performance."""
    probability = float(probability)
    if probability <= HIGH_PROBABILITY_THRESHOLD:
        return probability

    excess = probability - HIGH_PROBABILITY_THRESHOLD
    target_excess = OBSERVED_HIGH_PROBABILITY_HIT_RATE - HIGH_PROBABILITY_THRESHOLD
    shrinkage = target_excess / (1 - HIGH_PROBABILITY_THRESHOLD)
    return HIGH_PROBABILITY_THRESHOLD + excess * shrinkage


def calibrate_1x2(home: float, draw: float, away: float) -> dict:
    """Calibrate each outcome and renormalize to a valid 1X2 distribution."""
    raw = {
        "home": calibrated_probability(home),
        "draw": calibrated_probability(draw),
        "away": calibrated_probability(away),
    }
    total = sum(raw.values())
    if total <= 0:
        raise ValueError("Calibrated probabilities must sum to a positive value")
    return {key: value / total for key, value in raw.items()}

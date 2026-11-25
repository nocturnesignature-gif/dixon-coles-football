"""Per-outcome calibration for Dixon-Coles 1X2 probabilities."""

# Calibration is intentionally disabled until prospective validation is complete.
# The dashboard must display the raw corrected model probabilities.


def calibrate_1x2(home, draw, away, history=None):
    """Return the raw 1X2 probabilities without calibration."""
    home, draw, away = float(home), float(draw), float(away)
    total = home + draw + away
    if total <= 0:
        return {"home": 1 / 3, "draw": 1 / 3, "away": 1 / 3}
    return {
        "home": home / total,
        "draw": draw / total,
        "away": away / total,
    }

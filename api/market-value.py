"""Disabled market-value radar until prospective calibration is validated."""
from flask import Flask, jsonify

app = Flask(__name__)


@app.get("/api/market-value")
def market_value():
    return jsonify({
        "ok": True,
        "opportunities": [],
        "value_assessment": "disabled_pending_calibration",
        "reason": "Prospective calibration has not yet been validated.",
        "minimum_unique_prospective_observations": 100,
    })


if __name__ == "__main__":
    app.run()

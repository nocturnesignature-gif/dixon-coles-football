"""Read-only alert status endpoint; no actionable alerts are generated."""
from flask import Flask, jsonify

app = Flask(__name__)


@app.after_request
def no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/check-alerts")
def check_alerts():
    return jsonify({
        "ok": True,
        "status": "alerts_disabled",
        "alerts": [],
        "reason": "prospective_calibration_not_validated",
        "guardrails": {
            "bets": "disabled",
            "accumulators": "disabled",
            "notifications": "disabled",
            "recommendations": "disabled",
        },
        "limitations": [
            "This endpoint performs no betting action and sends no notification.",
            "Alerts remain disabled until prospective calibration is validated.",
        ],
    })


if __name__ == "__main__":
    app.run()

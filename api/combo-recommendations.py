"""Disabled accumulator recommendations until simple selections are validated."""
from flask import Flask, jsonify

app = Flask(__name__)


@app.get("/api/combo-recommendations")
def combo_recommendations():
    return jsonify({
        "ok": True,
        "combos": [],
        "enabled": False,
        "reason": "Simple selections must be prospectively validated before accumulators are enabled.",
    })


if __name__ == "__main__":
    app.run()

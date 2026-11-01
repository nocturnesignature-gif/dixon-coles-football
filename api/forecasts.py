"""Secure /api/forecasts endpoint backed by the Supabase catalogue."""
import json
import os
import urllib.request
from flask import Flask, jsonify, request

app = Flask(__name__)
CATALOGUE_URL = os.environ.get(
    "SUPABASE_CATALOGUE_URL",
    "https://ntnfcuzklqzeyrfunhtm.supabase.co/functions/v1/football-catalogue-v4",
)


@app.get("/api/forecasts")
def forecasts():
    profile_key = request.args.get("profile", "balanced").lower()
    try:
        query = "?limit=100&days=30"
        req = urllib.request.Request(CATALOGUE_URL + query, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=25) as response:
            payload = json.load(response)
    except Exception:
        return jsonify({"ok": False, "error": "catalogue_unavailable"}), 502

    matches = payload.get("matches", [])
    deduped = {}
    for match in matches:
        event_id = match.get("event_id") or match.get("catalogue_id")
        if event_id not in deduped:
            deduped[event_id] = match

    leagues = {}
    for match in deduped.values():
        competition = match.get("competition") or "unknown"
        leagues.setdefault(competition, []).append(match)

    return jsonify({
        "ok": True,
        "status": "experimental_unvalidated",
        "profile": profile_key,
        "source": "supabase_football_catalogue_v4",
        "calibration": {
            "status": "disabled_pending_calibration",
            "minimum_unique_prospective_observations": 100,
        },
        "value_assessment": "disabled_pending_calibration",
        "model_ready": False,
        "leagues": [
            {"competition": competition, "forecasts": forecasts}
            for competition, forecasts in sorted(leagues.items())
        ],
        "bets": [],
        "double_chance": [],
        "combos": [],
        "results": [],
        "guardrails": {
            "max_probability_edge_points": 10,
            "max_expected_value_percent": 15,
            "odds_limits": "profile_enforced",
            "combos": "disabled_until_simple_selections_validated",
        },
        "limitations": [
            "Value betting is disabled until prospective calibration is validated.",
            "Raw model probabilities are experimental and are not used for betting.",
            "No automatic bets, notifications, or accumulator recommendations.",
        ],
    })


if __name__ == "__main__":
    app.run()

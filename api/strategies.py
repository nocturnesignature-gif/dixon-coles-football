"""Strategy API using calibrated probabilities."""

import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path
from flask import Flask, jsonify, request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.calibration import calibrate_1x2

app = Flask(__name__)
LEAGUES = ["ligue_1", "premier_league", "la_liga", "bundesliga", "serie_a"]

STRATEGIES = {
    "favourite": "Favori 1X2",
    "high_probability": "Haute probabilité",
    "value": "Value betting",
    "double_chance": "Double chance",
    "draw": "Spécialiste du nul",
    "underdog": "Outsider",
    "prudent_combo": "Combiné prudent",
}


class SupabaseError(Exception):
    def __init__(self, status, body):
        super().__init__(f"Supabase HTTP {status}")
        self.status = status
        self.body = body


def supabase_get(path):
    base_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base_url or not service_key:
        raise RuntimeError("missing_supabase_config")
    req = urllib.request.Request(
        f"{base_url}/rest/v1/{path}",
        headers={"apikey": service_key, "Authorization": f"Bearer {service_key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            if response.status != 200:
                raise SupabaseError(response.status, "")
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise SupabaseError(exc.code, exc.read().decode(errors="replace")[:500]) from exc


def empty_metrics():
    return {"selections": 0, "average_probability": None, "average_expected_value": None, "average_clv": None, "clv_available": False}


def add(metrics, probability, ev, closing_odds=None, odds=None):
    metrics["selections"] += 1
    metrics["average_probability"] = (metrics["average_probability"] or 0) + probability
    metrics["average_expected_value"] = (metrics["average_expected_value"] or 0) + ev
    if closing_odds and odds:
        metrics["average_clv"] = (metrics["average_clv"] or 0) + (odds / closing_odds - 1)
        metrics["clv_available"] = True


def finalize(metrics):
    if not metrics["selections"]:
        return metrics
    count = metrics["selections"]
    metrics["average_probability"] /= count
    metrics["average_expected_value"] /= count
    if metrics["average_clv"] is not None:
        metrics["average_clv"] /= count
    return metrics


def evaluate(bets, doubles):
    results = {key: empty_metrics() for key in STRATEGIES}
    for bet in bets:
        probability, ev = bet["model_probability"], bet["expected_value"]
        if bet["market"] in ("home", "away"):
            same = [b for b in bets if b["event_id"] == bet["event_id"] and b["market"] in ("home", "away")]
            same.sort(key=lambda b: b["model_probability"], reverse=True)
            if same and same[0]["market"] == bet["market"]:
                add(results["favourite"], probability, ev, bet.get("closing_odds"), bet.get("odds"))
            if same and same[-1]["market"] == bet["market"] and ev > 0:
                add(results["underdog"], probability, ev, bet.get("closing_odds"), bet.get("odds"))
        if probability >= 0.65:
            add(results["high_probability"], probability, ev, bet.get("closing_odds"), bet.get("odds"))
        if ev >= 0.05:
            add(results["value"], probability, ev, bet.get("closing_odds"), bet.get("odds"))
        if bet["market"] == "draw" and probability == max(b["model_probability"] for b in bets if b["event_id"] == bet["event_id"]):
            add(results["draw"], probability, ev, bet.get("closing_odds"), bet.get("odds"))
    for bet in doubles:
        if bet["model_probability"] >= 0.70:
            add(results["double_chance"], bet["model_probability"], bet["expected_value"], bet.get("closing_odds"), bet.get("odds"))
    prudent = [b for b in bets + doubles if b["model_probability"] >= 0.65 and b["expected_value"] > 0]
    for bet in prudent[:20]:
        add(results["prudent_combo"], bet["model_probability"], bet["expected_value"], bet.get("closing_odds"), bet.get("odds"))
    return {key: finalize(value) for key, value in results.items()}


@app.get("/api/strategies")
def strategies():
    try:
        forecasts = supabase_get(
            "football_experimental_forecasts_v4?select=*"
            "&competition=in.(" + ",".join(LEAGUES) + ")"
            "&order=kickoff_at.asc&limit=1000"
        )
        odds_rows = supabase_get("live_odds?select=event_id,market,odds,captured_at&order=captured_at.desc&limit=1000")
        latest = {}
        for row in odds_rows:
            latest.setdefault((row["event_id"], row["market"]), row)
        bets, doubles = [], []
        for forecast in forecasts:
            event_id = forecast.get("external_id")
            probabilities = calibrate_1x2(forecast["home_probability"], forecast["draw_probability"], forecast["away_probability"])
            for market in ("home", "draw", "away"):
                odd = latest.get((event_id, market))
                if not odd:
                    continue
                ev = probabilities[market] * odd["odds"] - 1
                bets.append({"event_id": event_id, "market": market, "model_probability": probabilities[market], "expected_value": ev, "odds": odd["odds"], "closing_odds": None})
            for market, probability in {
                "1X": probabilities["home"] + probabilities["draw"],
                "12": probabilities["home"] + probabilities["away"],
                "X2": probabilities["draw"] + probabilities["away"],
            }.items():
                odd = latest.get((event_id, market))
                if not odd:
                    continue
                ev = probability * odd["odds"] - 1
                doubles.append({"event_id": event_id, "market": market, "model_probability": probability, "expected_value": ev, "odds": odd["odds"], "closing_odds": None})
        results = evaluate(bets, doubles)
        ranking = sorted(results.items(), key=lambda item: item[1]["average_expected_value"] or -1, reverse=True)
        eligible = [(key, value) for key, value in results.items() if value["selections"] and value["average_expected_value"] is not None]
        recommendation = max(eligible, key=lambda item: item[1]["average_expected_value"] + item[1]["average_probability"] / 10) if eligible else None
        return jsonify({
            "ok": True,
            "status": "experimental_unvalidated",
            "calibration_applied": True,
            "strategies": {key: {"label": STRATEGIES[key], **value} for key, value in results.items()},
            "ranking": [{"strategy": key, "label": STRATEGIES[key], **value} for key, value in ranking],
            "recommendation": {"strategy": recommendation[0], "label": STRATEGIES[recommendation[0]], **recommendation[1]} if recommendation else None,
        })
    except SupabaseError as exc:
        return jsonify({"ok": False, "error": f"supabase_http_{exc.status}", "details": exc.body}), 502
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

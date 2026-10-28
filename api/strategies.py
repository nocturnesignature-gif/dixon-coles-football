"""Strategy research: compare actionable betting strategies using model forecasts.

Strategies are evaluated on the latest available model forecasts and odds.
When closing odds are unavailable, CLV is reported as unavailable rather than
invented. The goal is to rank strategies by expected value, probability
quality, and risk—not to promise profits.
"""

import json
import os
import urllib.request
import urllib.error
from flask import Flask, jsonify, request

app = Flask(__name__)

LEAGUES = ["ligue_1", "premier_league", "la_liga", "bundesliga", "serie_a"]

STRATEGIES = {
    "favourite": {
        "label": "Favori 1X2",
        "description": "Parie le résultat avec la probabilité modèle la plus élevée.",
        "risk": "Faible à moyen",
    },
    "high_probability": {
        "label": "Haute probabilité",
        "description": "Parie uniquement si la probabilité modèle est au moins 65 %.",
        "risk": "Faible",
    },
    "value": {
        "label": "Value betting",
        "description": "Parie lorsque l’EV modèle est au moins 5 %.",
        "risk": "Moyen",
    },
    "double_chance": {
        "label": "Double chance",
        "description": "Retient la meilleure double chance si sa probabilité est au moins 70 %.",
        "risk": "Faible",
    },
    "draw": {
        "label": "Spécialiste du nul",
        "description": "Parie le nul lorsqu’il est le choix le plus probable.",
        "risk": "Élevé",
    },
    "underdog": {
        "label": "Outsider",
        "description": "Parie l’équipe extérieure ou domicile la moins probable, uniquement si son EV est positif.",
        "risk": "Élevé",
    },
    "prudent_combo": {
        "label": "Combiné prudent",
        "description": "Combiné de deux sélections à au moins 65 % de probabilité et EV positif.",
        "risk": "Moyen",
    },
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
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            if response.status != 200:
                raise SupabaseError(response.status, "")
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise SupabaseError(exc.code, exc.read().decode(errors="replace")[:500]) from exc


def empty_metrics():
    return {
        "selections": 0,
        "average_probability": None,
        "average_expected_value": None,
        "expected_roi": None,
        "average_clv": None,
        "clv_available": False,
        "variance_proxy": None,
        "risk": "—",
    }


def add_selection(metrics, probability, expected_value, odds=None, closing_odds=None):
    metrics["selections"] += 1
    metrics["average_probability"] = (metrics["average_probability"] or 0) + probability
    metrics["average_expected_value"] = (metrics["average_expected_value"] or 0) + expected_value
    if odds:
        metrics["variance_proxy"] = (metrics["variance_proxy"] or 0) + odds
    if closing_odds and odds:
        clv = odds / closing_odds - 1
        metrics["average_clv"] = (metrics["average_clv"] or 0) + clv
        metrics["clv_available"] = True


def finalize(metrics):
    count = metrics["selections"]
    if not count:
        return metrics
    metrics["average_probability"] /= count
    metrics["average_expected_value"] /= count
    metrics["expected_roi"] = metrics["average_expected_value"]
    if metrics["average_clv"] is not None:
        metrics["average_clv"] /= count
    if metrics["variance_proxy"] is not None:
        metrics["variance_proxy"] /= count
    return metrics


def evaluate(bets, double_chance):
    results = {key: empty_metrics() for key in STRATEGIES}

    for bet in bets:
        probability = bet["model_probability"]
        ev = bet["expected_value"]
        odds = bet.get("odds")
        closing = bet.get("closing_odds")

        if bet["market"] in ("home", "away"):
            probabilities = sorted(
                [b for b in bets if b["event_id"] == bet["event_id"] and b["market"] in ("home", "away")],
                key=lambda b: b["model_probability"],
                reverse=True,
            )
            if probabilities and probabilities[0]["market"] == bet["market"]:
                add_selection(results["favourite"], probability, ev, odds, closing)
            if probabilities and probabilities[-1]["market"] == bet["market"] and ev > 0:
                add_selection(results["underdog"], probability, ev, odds, closing)
        if probability >= 0.65:
            add_selection(results["high_probability"], probability, ev, odds, closing)
        if ev >= 0.05:
            add_selection(results["value"], probability, ev, odds, closing)

    for bet in double_chance:
        if bet["model_probability"] >= 0.70:
            add_selection(
                results["double_chance"],
                bet["model_probability"],
                bet["expected_value"],
                bet.get("odds"),
                bet.get("closing_odds"),
            )

    for bet in bets:
        if bet["market"] == "draw" and bet["model_probability"] == max(
            b["model_probability"] for b in bets if b["event_id"] == bet["event_id"]
        ):
            add_selection(results["draw"], bet["model_probability"], bet["expected_value"], bet.get("odds"), bet.get("closing_odds"))

    prudent = [b for b in bets + double_chance if b["model_probability"] >= 0.65 and b["expected_value"] > 0]
    for bet in prudent[:20]:
        add_selection(results["prudent_combo"], bet["model_probability"], bet["expected_value"], bet.get("odds"), bet.get("closing_odds"))

    for key, metrics in results.items():
        metrics["risk"] = STRATEGIES[key]["risk"]
        results[key] = finalize(metrics)
    return results


def recommend(results):
    eligible = [
        (key, metrics) for key, metrics in results.items()
        if metrics["selections"] > 0 and metrics["average_expected_value"] is not None
    ]
    if not eligible:
        return {
            "strategy": None,
            "label": None,
            "reason": "Aucune stratégie ne dispose actuellement de sélections suffisantes.",
        }

    def score(item):
        key, metrics = item
        clv_bonus = 0.02 if metrics["average_clv"] and metrics["average_clv"] > 0 else 0
        return metrics["average_expected_value"] + metrics["average_probability"] / 10 + clv_bonus

    best = max(eligible, key=score)
    key, metrics = best
    return {
        "strategy": key,
        "label": STRATEGIES[key]["label"],
        "reason": (
            f"Meilleur compromis actuel entre EV ({metrics['average_expected_value']:.1%}), "
            f"probabilité moyenne ({metrics['average_probability']:.1%}) et risque {metrics['risk']}."
        ),
    }


@app.get("/api/strategies")
def strategies():
    profile = request.args.get("profile", "balanced")
    try:
        forecasts = supabase_get(
            "football_experimental_forecasts_v4?select=*"
            "&competition=in.(" + ",".join(LEAGUES) + ")"
            "&order=kickoff_at.asc&limit=1000"
        )
        odds = supabase_get(
            "live_odds?select=event_id,market,odds,provider,captured_at"
            "&order=captured_at.desc&limit=1000"
        )
        latest_odds = {}
        for row in odds:
            latest_odds.setdefault((row["event_id"], row["market"]), row)

        bets = []
        double_chance = []
        for forecast in forecasts:
            event_id = forecast.get("external_id")
            probabilities = {
                "home": forecast["home_probability"],
                "draw": forecast["draw_probability"],
                "away": forecast["away_probability"],
            }
            for market in ("home", "draw", "away"):
                odd = latest_odds.get((event_id, market))
                if not odd:
                    continue
                ev = probabilities[market] * odd["odds"] - 1
                bets.append({
                    "event_id": event_id,
                    "market": market,
                    "model_probability": probabilities[market],
                    "expected_value": ev,
                    "odds": odd["odds"],
                    "closing_odds": None,
                })
            for market, probability in {
                "1X": probabilities["home"] + probabilities["draw"],
                "12": probabilities["home"] + probabilities["away"],
                "X2": probabilities["draw"] + probabilities["away"],
            }.items():
                odd = latest_odds.get((event_id, market))
                if not odd:
                    continue
                ev = probability * odd["odds"] - 1
                double_chance.append({
                    "event_id": event_id,
                    "market": market,
                    "model_probability": probability,
                    "expected_value": ev,
                    "odds": odd["odds"],
                    "closing_odds": None,
                })

        results = evaluate(bets, double_chance)
        ranking = sorted(
            results.items(),
            key=lambda item: (item[1]["average_expected_value"] or -1),
            reverse=True,
        )
        return jsonify({
            "ok": True,
            "status": "experimental_unvalidated",
            "profile": profile,
            "strategies": {
                key: {"label": STRATEGIES[key]["label"], "description": STRATEGIES[key]["description"], **metrics}
                for key, metrics in results.items()
            },
            "ranking": [
                {"strategy": key, "label": STRATEGIES[key]["label"], **metrics}
                for key, metrics in ranking
            ],
            "recommendation": recommend(results),
            "warning": "Les résultats ne constituent pas une garantie. Le CLV nécessite des cotes de clôture réelles.",
        })
    except SupabaseError as exc:
        return jsonify({"ok": False, "error": f"supabase_http_{exc.status}", "details": exc.body}), 502
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

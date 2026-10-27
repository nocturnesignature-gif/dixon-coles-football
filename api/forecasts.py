import json
import os
import urllib.request
import urllib.error
from itertools import combinations
from flask import Flask, jsonify

app = Flask(__name__)

LEAGUES = ["ligue_1", "premier_league", "la_liga", "bundesliga", "serie_a"]
RESULTS_VIEW = "football_model_prospective_results_v4"
MIN_EDGE = 0.03
MAX_COMBO_LEGS = 3
MAX_COMBOS = 10


def supabase_get(path: str):
    base_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base_url or not service_key:
        raise RuntimeError("Missing Supabase server environment configuration")
    url = f"{base_url}/rest/v1/{path}"
    request = urllib.request.Request(
        url,
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise RuntimeError(f"Supabase returned HTTP {response.status}")
        return json.load(response)


def kelly_fraction(expected_value, odds):
    if odds <= 1 or expected_value <= 0:
        return 0
    return min(0.02, 0.25 * expected_value / (odds - 1))


@app.get("/api/forecasts")
def forecasts():
    try:
        runs = supabase_get(
            "football_model_runs_v4?select=id,model_version,competition,status,created_at"
            "&competition=in.(" + ",".join(LEAGUES) + ")"
            "&order=created_at.desc&limit=100"
        )
        latest = {}
        for run in runs:
            latest.setdefault(run["competition"], run)
        versions = [run["model_version"] for run in latest.values()]
        forecasts = supabase_get(
            "football_experimental_forecasts_v4?select=*"
            "&model_version=in.(" + ",".join(versions) + ")"
            "&order=kickoff_at.asc"
        ) if versions else []
        odds = supabase_get(
            "live_odds?select=event_id,market,odds,provider,captured_at"
            "&order=captured_at.desc&limit=1000"
        )
        latest_odds = {}
        for row in odds:
            key = (row["event_id"], row["market"])
            latest_odds.setdefault(key, row)
        by_version = {}
        bets = []
        double_chance = []
        for forecast in forecasts:
            by_version.setdefault(forecast["model_version"], []).append(forecast)
            event_id = forecast.get("external_id")
            probabilities = {
                "home": forecast["home_probability"],
                "draw": forecast["draw_probability"],
                "away": forecast["away_probability"],
            }
            for market, outcome in (
                ("home", "Domicile"),
                ("draw", "Nul"),
                ("away", "Extérieur"),
            ):
                odd = latest_odds.get((event_id, market))
                if not odd:
                    continue
                probability = probabilities[market]
                expected_value = probability * odd["odds"] - 1
                if expected_value >= MIN_EDGE:
                    bets.append({
                        "event_id": event_id,
                        "competition": forecast["competition"],
                        "home_team": forecast["home_team"],
                        "away_team": forecast["away_team"],
                        "kickoff_at": forecast["kickoff_at"],
                        "market": market,
                        "outcome": outcome,
                        "model_probability": probability,
                        "odds": odd["odds"],
                        "implied_probability": 1 / odd["odds"],
                        "expected_value": expected_value,
                        "suggested_stake_fraction": kelly_fraction(expected_value, odd["odds"]),
                        "provider": odd["provider"],
                        "captured_at": odd["captured_at"],
                    })
            for market, outcome, probability in (
                ("1X", "Domicile ou nul", probabilities["home"] + probabilities["draw"]),
                ("12", "Domicile ou extérieur", probabilities["home"] + probabilities["away"]),
                ("X2", "Nul ou extérieur", probabilities["draw"] + probabilities["away"]),
            ):
                double_chance.append({
                    "event_id": event_id,
                    "competition": forecast["competition"],
                    "home_team": forecast["home_team"],
                    "away_team": forecast["away_team"],
                    "kickoff_at": forecast["kickoff_at"],
                    "market": market,
                    "outcome": outcome,
                    "model_probability": probability,
                    "minimum_odds": round(1 / probability, 3) if probability > 0 else None,
                })
        bets.sort(key=lambda x: x["expected_value"], reverse=True)
        double_chance.sort(key=lambda x: x["model_probability"], reverse=True)
        combos = []
        eligible = [bet for bet in bets if bet["expected_value"] > 0]
        for size in (2, 3):
            if size > MAX_COMBO_LEGS:
                break
            for selection in combinations(eligible, size):
                if len({bet["event_id"] for bet in selection}) != size:
                    continue
                probability = 1
                combined_odds = 1
                for bet in selection:
                    probability *= bet["model_probability"]
                    combined_odds *= bet["odds"]
                expected_value = probability * combined_odds - 1
                if expected_value < MIN_EDGE:
                    continue
                combos.append({
                    "legs": [
                        {
                            "event_id": bet["event_id"],
                            "match": f"{bet['home_team']} — {bet['away_team']}",
                            "outcome": bet["outcome"],
                            "model_probability": bet["model_probability"],
                            "odds": bet["odds"],
                        }
                        for bet in selection
                    ],
                    "combined_odds": combined_odds,
                    "model_probability": probability,
                    "expected_value": expected_value,
                    "suggested_stake_fraction": kelly_fraction(expected_value, combined_odds),
                })
        combos.sort(key=lambda x: x["expected_value"], reverse=True)
        combos = combos[:MAX_COMBOS]
        leagues = []
        for competition in LEAGUES:
            run = latest.get(competition)
            if not run:
                continue
            leagues.append({
                "competition": competition,
                "model": {
                    "id": run["id"],
                    "version": run["model_version"],
                    "status": run["status"],
                },
                "forecasts": by_version.get(run["model_version"], []),
            })
        results = supabase_get(f"{RESULTS_VIEW}?select=*&order=kickoff_at.asc")
        return jsonify({
            "ok": True,
            "status": "experimental_unvalidated",
            "leagues": leagues,
            "bets": bets,
            "double_chance": double_chance,
            "combos": combos,
            "results": results,
        })
    except urllib.error.HTTPError as exc:
        return jsonify({"ok": False, "error": f"upstream_http_{exc.code}"}), 502
    except (RuntimeError, urllib.error.URLError) as exc:
        return jsonify({"ok": False, "error": "configuration_or_upstream_error"}), 500

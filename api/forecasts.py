import json
import os
import urllib.request
import urllib.error
from flask import Flask, jsonify

app = Flask(__name__)

LEAGUES = ["ligue_1", "premier_league", "la_liga", "bundesliga", "serie_a"]
RESULTS_VIEW = "football_model_prospective_results_v4"


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
        opportunities = []
        for forecast in forecasts:
            by_version.setdefault(forecast["model_version"], []).append(forecast)
            event_id = forecast.get("external_id")
            for market, probability_key, outcome in (
                ("home", "home_probability", "Domicile"),
                ("draw", "draw_probability", "Nul"),
                ("away", "away_probability", "Extérieur"),
            ):
                odd = latest_odds.get((event_id, market))
                if not odd:
                    continue
                probability = forecast[probability_key]
                implied = 1 / odd["odds"]
                expected_value = probability * odd["odds"] - 1
                if expected_value >= 0.03:
                    opportunities.append({
                        "event_id": event_id,
                        "competition": forecast["competition"],
                        "home_team": forecast["home_team"],
                        "away_team": forecast["away_team"],
                        "market": market,
                        "outcome": outcome,
                        "model_probability": probability,
                        "odds": odd["odds"],
                        "implied_probability": implied,
                        "expected_value": expected_value,
                        "provider": odd["provider"],
                        "captured_at": odd["captured_at"],
                    })
        opportunities.sort(key=lambda x: x["expected_value"], reverse=True)
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
            "opportunities": opportunities,
            "results": results,
        })
    except urllib.error.HTTPError as exc:
        return jsonify({"ok": False, "error": f"upstream_http_{exc.code}"}), 502
    except (RuntimeError, urllib.error.URLError) as exc:
        return jsonify({"ok": False, "error": "configuration_or_upstream_error"}), 500

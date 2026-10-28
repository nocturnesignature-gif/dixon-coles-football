import json
import os
import urllib.request
import urllib.error
from itertools import combinations
from flask import Flask, jsonify, request

app = Flask(__name__)

LEAGUES = ["ligue_1", "premier_league", "la_liga", "bundesliga", "serie_a"]
RESULTS_VIEW = "football_model_prospective_results_v4"
MIN_ODDS = 1.45
MAX_COMBO_LEGS = 3
MAX_COMBOS = 10

PROFILES = {
    "prudent": {
        "label": "Prudent",
        "minimum_single_probability": 0.65,
        "minimum_expected_value": 0.03,
        "maximum_odds": 2.00,
        "minimum_double_chance_probability": 0.70,
        "minimum_double_chance_expected_value": 0.02,
        "minimum_combo_probability": 0.45,
        "minimum_combo_expected_value": 0.03,
        "maximum_combo_odds": 3.00,
        "maximum_combo_legs": 2,
    },
    "balanced": {
        "label": "Équilibré",
        "minimum_single_probability": 0.55,
        "minimum_expected_value": 0.05,
        "maximum_odds": 3.80,
        "minimum_double_chance_probability": 0.65,
        "minimum_double_chance_expected_value": 0.03,
        "minimum_combo_probability": 0.30,
        "minimum_combo_expected_value": 0.05,
        "maximum_combo_odds": 5.00,
        "maximum_combo_legs": 3,
    },
    "value": {
        "label": "Valeur",
        "minimum_single_probability": 0.45,
        "minimum_expected_value": 0.10,
        "maximum_odds": 5.00,
        "minimum_double_chance_probability": 0.60,
        "minimum_double_chance_expected_value": 0.05,
        "minimum_combo_probability": 0.20,
        "minimum_combo_expected_value": 0.08,
        "maximum_combo_odds": 8.00,
        "maximum_combo_legs": 3,
    },
}


class SupabaseError(Exception):
    def __init__(self, status, url, body):
        super().__init__(f"Supabase HTTP {status}")
        self.status = status
        self.url = url
        self.body = body


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
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status != 200:
                raise SupabaseError(response.status, url, "")
            return json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:500]
        raise SupabaseError(exc.code, url, body) from exc


def kelly_fraction(expected_value, odds):
    if odds <= 1 or expected_value <= 0:
        return 0
    return min(0.02, 0.25 * expected_value / (odds - 1))


def confidence(probability):
    if probability >= 0.65:
        return "Élevée"
    if probability >= 0.55:
        return "Moyenne"
    return "Faible"


def selection_score(item):
    return item["expected_value"] * item["model_probability"]


@app.get("/api/forecasts")
def forecasts():
    profile_key = request.args.get("profile", "balanced").lower()
    if profile_key not in PROFILES:
        return jsonify({"ok": False, "error": "invalid_profile", "valid_profiles": list(PROFILES)}), 400
    profile = PROFILES[profile_key]

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
            latest_odds.setdefault((row["event_id"], row["market"]), row)
        by_version = {}
        bets = []
        double_chance_by_event = {}
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
                if not (
                    expected_value >= profile["minimum_expected_value"]
                    and MIN_ODDS <= odd["odds"] <= profile["maximum_odds"]
                    and probability >= profile["minimum_single_probability"]
                ):
                    continue
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
                    "confidence": confidence(probability),
                    "suggested_stake_fraction": kelly_fraction(expected_value, odd["odds"]),
                    "provider": odd["provider"],
                    "captured_at": odd["captured_at"],
                })
            dc_probabilities = {
                "1X": ("Domicile ou nul", probabilities["home"] + probabilities["draw"]),
                "12": ("Domicile ou extérieur", probabilities["home"] + probabilities["away"]),
                "X2": ("Nul ou extérieur", probabilities["draw"] + probabilities["away"]),
            }
            best_dc = None
            for market, (outcome, probability) in dc_probabilities.items():
                odd = latest_odds.get((event_id, market))
                if not odd:
                    continue
                expected_value = probability * odd["odds"] - 1
                if (
                    probability < profile["minimum_double_chance_probability"]
                    or expected_value < profile["minimum_double_chance_expected_value"]
                ):
                    continue
                candidate = {
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
                    "confidence": confidence(probability),
                    "suggested_stake_fraction": kelly_fraction(expected_value, odd["odds"]),
                    "provider": odd["provider"],
                    "captured_at": odd["captured_at"],
                }
                if best_dc is None or selection_score(candidate) > selection_score(best_dc):
                    best_dc = candidate
            if best_dc:
                double_chance_by_event[event_id] = best_dc
        double_chance = sorted(double_chance_by_event.values(), key=selection_score, reverse=True)
        bets.sort(key=selection_score, reverse=True)
        combo_candidates = bets + double_chance
        combo_candidates.sort(key=selection_score, reverse=True)
        combos = []
        max_legs = min(profile["maximum_combo_legs"], MAX_COMBO_LEGS)
        for size in range(2, max_legs + 1):
            for selection in combinations(combo_candidates, size):
                if len({bet["event_id"] for bet in selection}) != size:
                    continue
                probability = 1
                combined_odds = 1
                for bet in selection:
                    probability *= bet["model_probability"]
                    combined_odds *= bet["odds"]
                expected_value = probability * combined_odds - 1
                if (
                    probability < profile["minimum_combo_probability"]
                    or combined_odds > profile["maximum_combo_odds"]
                    or expected_value < profile["minimum_combo_expected_value"]
                ):
                    continue
                combos.append({
                    "legs": [
                        {
                            "event_id": bet["event_id"],
                            "match": f"{bet['home_team']} — {bet['away_team']}",
                            "market": bet["market"],
                            "outcome": bet["outcome"],
                            "model_probability": bet["model_probability"],
                            "odds": bet["odds"],
                        }
                        for bet in selection
                    ],
                    "combined_odds": combined_odds,
                    "model_probability": probability,
                    "expected_value": expected_value,
                    "confidence": confidence(probability),
                    "suggested_stake_fraction": kelly_fraction(expected_value, combined_odds),
                })
        combos.sort(key=selection_score, reverse=True)
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
            "profile": profile_key,
            "profile_label": profile["label"],
            "available_profiles": {
                key: {"label": value["label"], **value} for key, value in PROFILES.items()
            },
            "filters": profile,
            "leagues": leagues,
            "bets": bets,
            "double_chance": double_chance,
            "combos": combos,
            "results": results,
        })
    except SupabaseError as exc:
        return jsonify({"ok": False, "error": f"supabase_http_{exc.status}", "details": exc.body}), 502
    except (RuntimeError, urllib.error.URLError) as exc:
        return jsonify({"ok": False, "error": "configuration_or_upstream_error"}), 500

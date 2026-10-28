"""Forecast API with calibrated probabilities and risk controls."""

import json
import os
import urllib.request
import urllib.error
from itertools import combinations
from flask import Flask, jsonify, request

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.calibration import calibrate_1x2

app = Flask(__name__)

LEAGUES = ["ligue_1", "premier_league", "la_liga", "bundesliga", "serie_a"]
RESULTS_VIEW = "football_model_prospective_results_v4"
MIN_ODDS = 1.45
MAX_COMBO_LEGS = 3
MAX_COMBOS = 10
MAX_KELLY_FRACTION = 0.01
MAX_DAILY_EXPOSURE_FRACTION = 0.05

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


def supabase_get(path):
    base_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base_url or not service_key:
        raise RuntimeError("Missing Supabase server environment configuration")
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
                raise SupabaseError(response.status, "", "")
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise SupabaseError(exc.code, "", exc.read().decode(errors="replace")[:500]) from exc


def kelly_fraction(expected_value, odds):
    if odds <= 1 or expected_value <= 0:
        return 0
    return min(MAX_KELLY_FRACTION, 0.25 * expected_value / (odds - 1))


def confidence(probability):
    if probability >= 0.65:
        return "Élevée"
    if probability >= 0.55:
        return "Moyenne"
    return "Faible"


def score(item):
    return item["expected_value"] * item["model_probability"]


def calibrated_probabilities(forecast):
    return calibrate_1x2(
        forecast["home_probability"],
        forecast["draw_probability"],
        forecast["away_probability"],
    )


@app.get("/api/forecasts")
def forecasts():
    profile_key = request.args.get("profile", "balanced").lower()
    if profile_key not in PROFILES:
        return jsonify({"ok": False, "error": "invalid_profile"}), 400
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
        odds_rows = supabase_get(
            "live_odds?select=event_id,market,odds,provider,captured_at"
            "&order=captured_at.desc&limit=1000"
        )
        latest_odds = {}
        for row in odds_rows:
            latest_odds.setdefault((row["event_id"], row["market"]), row)

        by_version = {}
        bets = []
        double_chance_by_event = {}
        for forecast in forecasts:
            by_version.setdefault(forecast["model_version"], []).append(forecast)
            event_id = forecast.get("external_id")
            probabilities = calibrated_probabilities(forecast)

            for market, outcome in (("home", "Domicile"), ("draw", "Nul"), ("away", "Extérieur")):
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
                    "raw_model_probability": forecast[f"{market}_probability"],
                    "odds": odd["odds"],
                    "expected_value": expected_value,
                    "confidence": confidence(probability),
                    "suggested_stake_fraction": kelly_fraction(expected_value, odd["odds"]),
                    "provider": odd["provider"],
                    "captured_at": odd["captured_at"],
                })

            dc = {
                "1X": probabilities["home"] + probabilities["draw"],
                "12": probabilities["home"] + probabilities["away"],
                "X2": probabilities["draw"] + probabilities["away"],
            }
            best = None
            for market, probability in dc.items():
                odd = latest_odds.get((event_id, market))
                if not odd:
                    continue
                expected_value = probability * odd["odds"] - 1
                if probability < profile["minimum_double_chance_probability"] or expected_value < profile["minimum_double_chance_expected_value"]:
                    continue
                candidate = {
                    "event_id": event_id,
                    "competition": forecast["competition"],
                    "home_team": forecast["home_team"],
                    "away_team": forecast["away_team"],
                    "kickoff_at": forecast["kickoff_at"],
                    "market": market,
                    "outcome": market,
                    "model_probability": probability,
                    "odds": odd["odds"],
                    "expected_value": expected_value,
                    "confidence": confidence(probability),
                    "suggested_stake_fraction": kelly_fraction(expected_value, odd["odds"]),
                    "provider": odd["provider"],
                    "captured_at": odd["captured_at"],
                }
                if best is None or score(candidate) > score(best):
                    best = candidate
            if best:
                double_chance_by_event[event_id] = best

        double_chance = sorted(double_chance_by_event.values(), key=score, reverse=True)
        bets.sort(key=score, reverse=True)
        candidates = bets + double_chance
        candidates.sort(key=score, reverse=True)
        combos = []
        for size in range(2, min(profile["maximum_combo_legs"], MAX_COMBO_LEGS) + 1):
            for selection in combinations(candidates, size):
                if len({b["event_id"] for b in selection}) != size:
                    continue
                probability = 1
                combined_odds = 1
                for bet in selection:
                    probability *= bet["model_probability"]
                    combined_odds *= bet["odds"]
                expected_value = probability * combined_odds - 1
                if probability < profile["minimum_combo_probability"] or combined_odds > profile["maximum_combo_odds"] or expected_value < profile["minimum_combo_expected_value"]:
                    continue
                combos.append({
                    "legs": [{
                        "event_id": b["event_id"],
                        "match": f"{b['home_team']} — {b['away_team']}",
                        "market": b["market"],
                        "outcome": b["outcome"],
                        "model_probability": b["model_probability"],
                        "odds": b["odds"],
                    } for b in selection],
                    "combined_odds": combined_odds,
                    "model_probability": probability,
                    "expected_value": expected_value,
                    "confidence": confidence(probability),
                    "suggested_stake_fraction": kelly_fraction(expected_value, combined_odds),
                })
        combos.sort(key=score, reverse=True)
        combos = combos[:MAX_COMBOS]

        leagues = []
        for competition in LEAGUES:
            run = latest.get(competition)
            if not run:
                continue
            leagues.append({
                "competition": competition,
                "model": {"id": run["id"], "version": run["model_version"], "status": run["status"]},
                "forecasts": by_version.get(run["model_version"], []),
            })
        results = supabase_get(f"{RESULTS_VIEW}?select=*&order=kickoff_at.asc")
        return jsonify({
            "ok": True,
            "status": "experimental_unvalidated",
            "profile": profile_key,
            "profile_label": profile["label"],
            "calibration": {
                "applied": True,
                "high_probability_threshold": 0.65,
                "observed_high_probability_hit_rate": 0.6412451361867705,
            },
            "risk_controls": {
                "maximum_kelly_fraction": MAX_KELLY_FRACTION,
                "maximum_daily_exposure_fraction": MAX_DAILY_EXPOSURE_FRACTION,
            },
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

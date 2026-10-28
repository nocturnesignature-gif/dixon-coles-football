"""Forecast API with per-outcome historical calibration and risk controls."""

import json
import os
import urllib.request
import urllib.error
from flask import Flask, jsonify, request

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.calibration import calibrate_1x2

app = Flask(__name__)

LEAGUES = ["ligue_1", "premier_league", "la_liga", "bundesliga", "serie_a"]
RESULTS_VIEW = "football_model_prospective_results_v4"
MIN_ODDS = 1.45
MAX_COMBOS = 10
MAX_KELLY_FRACTION = 0.01
MAX_DAILY_EXPOSURE_FRACTION = 0.05
MAX_EXPECTED_VALUE = 0.15
MIN_PLAUSIBLE_PROBABILITY = 0.02
MAX_PLAUSIBLE_PROBABILITY = 0.85

PROFILES = {
    "prudent": {
        "label": "Prudent",
        "minimum_single_probability": 0.65,
        "minimum_expected_value": 0.03,
        "maximum_odds": 2.00,
    },
    "balanced": {
        "label": "Équilibré",
        "minimum_single_probability": 0.55,
        "minimum_expected_value": 0.05,
        "maximum_odds": 3.80,
    },
    "value": {
        "label": "Valeur",
        "minimum_single_probability": 0.45,
        "minimum_expected_value": 0.10,
        "maximum_odds": 5.00,
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


def implausible(forecast):
    values = [
        forecast["home_probability"],
        forecast["draw_probability"],
        forecast["away_probability"],
    ]
    return any(v is None or v < MIN_PLAUSIBLE_PROBABILITY or v > MAX_PLAUSIBLE_PROBABILITY for v in values)


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

        try:
            history = supabase_get(
                f"{RESULTS_VIEW}?select=home_probability,draw_probability,away_probability,actual_outcome"
                "&actual_outcome=not.is.null"
                "&order=kickoff_at.desc&limit=500"
            )
        except SupabaseError:
            history = []

        by_version = {}
        bets = []
        for forecast in forecasts:
            by_version.setdefault(forecast["model_version"], []).append(forecast)
            event_id = forecast.get("external_id")
            probabilities = calibrate_1x2(
                forecast["home_probability"],
                forecast["draw_probability"],
                forecast["away_probability"],
                history,
            )
            forecast["calibrated_home_probability"] = probabilities["home"]
            forecast["calibrated_draw_probability"] = probabilities["draw"]
            forecast["calibrated_away_probability"] = probabilities["away"]
            forecast["quality_flag"] = "implausible_raw_probabilities" if implausible(forecast) else "ok"

            if forecast["quality_flag"] != "ok":
                continue

            for market, outcome in (("home", "Domicile"), ("draw", "Nul"), ("away", "Extérieur")):
                odd = latest_odds.get((event_id, market))
                if not odd:
                    continue
                probability = probabilities[market]
                expected_value = probability * odd["odds"] - 1
                if not (
                    expected_value >= profile["minimum_expected_value"]
                    and expected_value <= MAX_EXPECTED_VALUE
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

        bets.sort(key=score, reverse=True)

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
                "method": "per_outcome_logistic" if len(history) >= 50 else "legacy_high_probability_shrinkage",
                "history_rows": len(history),
                "maximum_expected_value": MAX_EXPECTED_VALUE,
                "plausible_probability_range": [MIN_PLAUSIBLE_PROBABILITY, MAX_PLAUSIBLE_PROBABILITY],
            },
            "risk_controls": {
                "maximum_kelly_fraction": MAX_KELLY_FRACTION,
                "maximum_daily_exposure_fraction": MAX_DAILY_EXPOSURE_FRACTION,
            },
            "leagues": leagues,
            "bets": bets,
            "double_chance": [],
            "combos": [],
            "results": results,
        })
    except SupabaseError as exc:
        return jsonify({"ok": False, "error": f"supabase_http_{exc.status}", "details": exc.body}), 502
    except (RuntimeError, urllib.error.URLError) as exc:
        return jsonify({"ok": False, "error": "configuration_or_upstream_error"}), 500

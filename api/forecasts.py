import json
import os
import urllib.request
import urllib.error
from datetime import datetime
from flask import Flask, jsonify, request

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.calibration import calibrate_1x2

app = Flask(__name__)

LEAGUES = [
    "ligue_1",
    "premier_league",
    "la_liga",
    "bundesliga",
    "serie_a",
    "championship",
    "eredivisie",
    "primeira_liga",
]

LEAGUE_EVENT_ALIASES = {
    "ligue_1": ["Ligue 1"],
    "premier_league": ["Premier League"],
    "la_liga": ["Primera Division", "LaLiga"],
    "bundesliga": ["Bundesliga"],
    "serie_a": ["Serie A"],
    "championship": ["Championship"],
    "eredivisie": ["Eredivisie"],
    "primeira_liga": ["Primeira Liga"],
}

RESULTS_VIEW = "football_prospective_verified_scores_v1"
MIN_ODDS = 1.45
MAX_KELLY_FRACTION = 0.01
MAX_EXPECTED_VALUE = 0.15
MIN_PLAUSIBLE_PROBABILITY = 0.02
MAX_PLAUSIBLE_PROBABILITY = 0.85
MIN_HISTORY_FOR_FITTING = 50
MAX_ODDS_AGE_HOURS = 6

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


def supabase_request(path):
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
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"supabase_http_{exc.code}:{path}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError("supabase_request_failed") from exc


def parse_datetime(value):
    return datetime.fromisoformat((value or "").replace("Z", "+00:00"))


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


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


def implausible(forecast):
    values = [
        forecast.get("home_probability"),
        forecast.get("draw_probability"),
        forecast.get("away_probability"),
    ]
    return any(
        value is None
        or float(value) < MIN_PLAUSIBLE_PROBABILITY
        or float(value) > MAX_PLAUSIBLE_PROBABILITY
        for value in values
    )


def fresh_odds(odd, kickoff):
    captured = parse_datetime(odd.get("captured_at"))
    return (
        captured <= kickoff
        and (kickoff - captured).total_seconds() <= MAX_ODDS_AGE_HOURS * 3600
    )


@app.get("/api/forecasts")
def forecasts():
    profile_key = request.args.get("profile", "balanced").lower()

    if profile_key not in PROFILES:
        return jsonify({"ok": False, "error": "invalid_profile"}), 400

    profile = PROFILES[profile_key]

    try:
        runs = supabase_request(
            "football_model_runs_v4"
            "?select=id,model_version,competition,status,created_at"
            "&competition=in.(" + ",".join(LEAGUES) + ")"
            "&order=created_at.desc&limit=100"
        )

        rows = supabase_request(
            "football_experimental_forecasts_v4?select=*"
            "&order=kickoff_at.asc&limit=1000"
        )

        odds_rows = supabase_request(
            "live_odds?select=event_id,market,odds,provider,captured_at"
            "&order=captured_at.desc&limit=1000"
        )

        latest_odds = {}
        for row in odds_rows:
            latest_odds.setdefault(
                (row["event_id"], row["market"]),
                row,
            )

        try:
            history = supabase_request(
                f"{RESULTS_VIEW}"
                "?select=home_probability,draw_probability,away_probability,"
                "actual_outcome&order=kickoff_at.desc&limit=500"
            )
        except RuntimeError:
            history = []

        by_version = {}
        bets = []

        for forecast in rows:
            forecast["protected_forecast"] = bool(
                forecast.get("recorded_at")
            )
            by_version.setdefault(
                forecast["model_version"],
                [],
            ).append(forecast)

            event_id = forecast.get("external_id")
            kickoff = parse_datetime(forecast.get("kickoff_at"))

            probabilities = calibrate_1x2(
                forecast["home_probability"],
                forecast["draw_probability"],
                forecast["away_probability"],
                history,
            )

            forecast.update(
                {
                    "calibrated_home_probability": probabilities["home"],
                    "calibrated_draw_probability": probabilities["draw"],
                    "calibrated_away_probability": probabilities["away"],
                    "quality_flag": (
                        "implausible_raw_probabilities"
                        if implausible(forecast)
                        else "ok"
                    ),
                }
            )

            if forecast["quality_flag"] != "ok":
                continue

            for market, outcome in (
                ("home", "Domicile"),
                ("draw", "Nul"),
                ("away", "Extérieur"),
            ):
                odd = latest_odds.get((event_id, market))

                if not odd or not fresh_odds(odd, kickoff):
                    continue

                probability = probabilities[market]
                expected_value = probability * float(odd["odds"]) - 1

                if not (
                    expected_value >= profile["minimum_expected_value"]
                    and expected_value <= MAX_EXPECTED_VALUE
                    and MIN_ODDS <= float(odd["odds"]) <= profile["maximum_odds"]
                    and probability >= profile["minimum_single_probability"]
                ):
                    continue

                bets.append(
                    {
                        "event_id": event_id,
                        "competition": forecast["competition"],
                        "home_team": forecast["home_team"],
                        "away_team": forecast["away_team"],
                        "kickoff_at": forecast["kickoff_at"],
                        "market": market,
                        "outcome": outcome,
                        "model_probability": probability,
                        "raw_model_probability": forecast[
                            f"{market}_probability"
                        ],
                        "odds": odd["odds"],
                        "expected_value": expected_value,
                        "confidence": confidence(probability),
                        "suggested_stake_fraction": kelly_fraction(
                            expected_value,
                            float(odd["odds"]),
                        ),
                        "provider": odd["provider"],
                        "captured_at": odd["captured_at"],
                        "odds_age_hours": round(
                            (
                                kickoff
                                - parse_datetime(odd["captured_at"])
                            ).total_seconds()
                            / 3600,
                            2,
                        ),
                        "protected_forecast": forecast[
                            "protected_forecast"
                        ],
                    }
                )

        bets.sort(
            key=lambda bet: bet["expected_value"] * bet["model_probability"],
            reverse=True,
        )

        leagues = []

        for league in LEAGUES:
            aliases = LEAGUE_EVENT_ALIASES.get(league, [])

            eligible_runs = [
                run
                for run in runs
                if run["competition"] == league
                and any(
                    forecast.get("competition") in aliases
                    for forecast in by_version.get(
                        run["model_version"],
                        [],
                    )
                )
            ]

            if not eligible_runs:
                continue

            run = eligible_runs[0]
            league_forecasts = [
                forecast
                for forecast in by_version.get(run["model_version"], [])
                if forecast.get("competition") in aliases
            ]

            leagues.append(
                {
                    "competition": league,
                    "event_competitions": aliases,
                    "model": {
                        "id": run["id"],
                        "version": run["model_version"],
                        "status": run["status"],
                    },
                    "forecasts": league_forecasts,
                }
            )

        try:
            results = supabase_request(
                f"{RESULTS_VIEW}?select=*&order=kickoff_at.asc"
            )
        except RuntimeError:
            results = []

        return jsonify(
            {
                "ok": True,
                "status": "experimental_unvalidated",
                "profile": profile_key,
                "profile_label": profile["label"],
                "calibration": {
                    "method": (
                        "per_outcome_logistic"
                        if len(history) >= MIN_HISTORY_FOR_FITTING
                        else "legacy_high_probability_shrinkage"
                    ),
                    "history_rows": len(history),
                    "maximum_expected_value": MAX_EXPECTED_VALUE,
                    "plausible_probability_range": [
                        MIN_PLAUSIBLE_PROBABILITY,
                        MAX_PLAUSIBLE_PROBABILITY,
                    ],
                },
                "risk_controls": {
                    "maximum_kelly_fraction": MAX_KELLY_FRACTION,
                    "maximum_daily_exposure_fraction": 0.05,
                    "maximum_odds_age_hours": MAX_ODDS_AGE_HOURS,
                },
                "leagues": leagues,
                "bets": bets,
                "double_chance": [],
                "combos": [],
                "results": results,
            }
        )

    except Exception as exc:
        return jsonify(
            {
                "ok": False,
                "error": "forecasts_unavailable",
                "details": str(exc),
            }
        ), 502

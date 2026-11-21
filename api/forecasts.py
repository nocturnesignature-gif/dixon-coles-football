from api.index import (
    supabase_request,
    parse_datetime,
    calibrate_1x2,
    implausible,
    PROFILES,
    LEAGUES,
    LEAGUE_EVENT_ALIASES,
    RESULTS_VIEW,
    MIN_ODDS,
    MAX_KELLY_FRACTION,
    MAX_EXPECTED_VALUE,
    MIN_PLAUSIBLE_PROBABILITY,
    MAX_PLAUSIBLE_PROBABILITY,
    MIN_HISTORY_FOR_FITTING,
    MAX_ODDS_AGE_HOURS,
    kelly_fraction,
    confidence,
    fresh_odds,
)
from flask import Flask, jsonify, request

app = Flask(__name__)


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
        except Exception:
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
                expected_value = probability * odd["odds"] - 1

                if not (
                    expected_value >= profile["minimum_expected_value"]
                    and expected_value <= MAX_EXPECTED_VALUE
                    and MIN_ODDS <= odd["odds"] <= profile["maximum_odds"]
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
                            odd["odds"],
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

        results = supabase_request(
            f"{RESULTS_VIEW}?select=*&order=kickoff_at.asc"
        )

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

"""Read-only data maturity analytics for historical odds coverage."""
import json
import os
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime
from flask import Flask, jsonify

app = Flask(__name__)
MIN_COMPLETE_OBSERVATIONS = 100


def supabase(path):
    base = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base or not key:
        raise RuntimeError("missing_supabase_config")
    req = urllib.request.Request(
        f"{base}/rest/v1/{path}",
        headers={"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


@app.after_request
def no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


def parse_timestamp(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@app.get("/api/data-maturity")
def data_maturity():
    try:
        rows = supabase(
            "historical_match_odds?select=match_date,league,home_team,away_team,market,"
            "bookmaker,odds_type,odds,captured_at&order=captured_at.desc&limit=10000"
        )
    except RuntimeError:
        return jsonify({"ok": False, "error": "missing_supabase_config"}), 500
    except urllib.error.HTTPError as exc:
        return jsonify({"ok": False, "error": f"supabase_http_{exc.code}"}), 502
    except (urllib.error.URLError, TimeoutError, ValueError):
        return jsonify({"ok": False, "error": "supabase_unavailable"}), 502

    groups = defaultdict(lambda: {
        "prediction": set(), "closing": set(),
        "prediction_captured_at": [], "closing_captured_at": [],
        "bookmakers": set(), "total_rows": 0,
    })
    for row in rows:
        key = (
            row.get("league"), row.get("home_team"), row.get("away_team"),
            row.get("match_date"), row.get("market"), row.get("bookmaker"),
        )
        group = groups[key]
        odds_type = row.get("odds_type")
        if odds_type not in {"prediction", "closing"}:
            continue
        group[odds_type].add(row.get("odds"))
        group[f"{odds_type}_captured_at"].append(row.get("captured_at"))
        group["bookmakers"].add(row.get("bookmaker"))
        group["total_rows"] += 1

    by_league = defaultdict(lambda: {
        "complete_pairs": 0, "prediction_only": 0, "closing_only": 0,
        "total_selections": 0, "bookmakers": set(), "latest_capture": None,
    })
    for key, group in groups.items():
        league = key[0]
        summary = by_league[league]
        summary["total_selections"] += 1
        summary["bookmakers"].update(group["bookmakers"])
        captures = group["prediction_captured_at"] + group["closing_captured_at"]
        latest = max((parse_timestamp(x) for x in captures if x), default=None)
        if latest and (summary["latest_capture"] is None or latest > parse_timestamp(summary["latest_capture"])):
            summary["latest_capture"] = latest.isoformat()
        if group["prediction"] and group["closing"]:
            summary["complete_pairs"] += 1
        elif group["prediction"]:
            summary["prediction_only"] += 1
        elif group["closing"]:
            summary["closing_only"] += 1

    leagues = []
    for league, values in sorted(by_league.items()):
        complete = values["complete_pairs"]
        leagues.append({
            "league": league,
            "complete_pairs": complete,
            "prediction_only": values["prediction_only"],
            "closing_only": values["closing_only"],
            "total_selections": values["total_selections"],
            "completion_rate": complete / values["total_selections"] if values["total_selections"] else 0,
            "bookmaker_count": len(values["bookmakers"]),
            "latest_capture": values["latest_capture"],
            "financial_backtesting_enabled": complete >= MIN_COMPLETE_OBSERVATIONS,
            "progress_to_threshold": min(complete / MIN_COMPLETE_OBSERVATIONS, 1),
        })

    total_complete = sum(item["complete_pairs"] for item in leagues)
    total_selections = sum(item["total_selections"] for item in leagues)
    return jsonify({
        "ok": True,
        "status": "analysis_only",
        "thresholds": {
            "minimum_complete_observations_per_league": MIN_COMPLETE_OBSERVATIONS,
            "required_odds_types": ["prediction", "closing"],
        },
        "overall": {
            "complete_pairs": total_complete,
            "total_selections": total_selections,
            "completion_rate": total_complete / total_selections if total_selections else 0,
            "leagues_ready_for_financial_backtesting": sum(i["financial_backtesting_enabled"] for i in leagues),
            "latest_capture": max((i["latest_capture"] for i in leagues if i["latest_capture"]), default=None),
        },
        "leagues": leagues,
        "limitations": [
            "A complete pair requires both a prediction odds capture and a closing odds capture.",
            "Financial metrics remain disabled until the per-league threshold is reached.",
            "Data coverage does not validate model quality or betting profitability.",
            "No bet, stake, accumulator, alert or trading recommendation is generated.",
        ],
    })


if __name__ == "__main__":
    app.run()

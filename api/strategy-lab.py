"""Read-only Strategy Lab backtesting; no betting recommendations."""
import json
import os
import urllib.error
import urllib.request
from collections import defaultdict
from flask import Flask, jsonify, request

app = Flask(__name__)


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


def clamp(value, low, high):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return max(low, min(high, value))


@app.get("/api/strategy-lab")
def strategy_lab():
    league = (request.args.get("league") or "").strip()
    market = (request.args.get("market") or "home").lower()
    date_from = (request.args.get("date_from") or "").strip()
    date_to = (request.args.get("date_to") or "").strip()
    min_probability = clamp(request.args.get("min_probability", 0), 0, 1) or 0
    max_probability = clamp(request.args.get("max_probability", 1), 0, 1)
    if max_probability is None:
        max_probability = 1
    if market not in {"home", "draw", "away"}:
        return jsonify({"ok": False, "error": "invalid_market"}), 400

    filters = [
        "select=match_date,league,home_team,away_team,home_goals,away_goals,actual_outcome,"
        "home_probability,draw_probability,away_probability,log_loss,brier_score,model_version",
        "order=match_date.asc",
        "limit=5000",
    ]
    if league:
        filters.append(f"league=eq.{urllib.request.quote(league)}")
    if date_from:
        filters.append(f"match_date=gte.{date_from}")
    if date_to:
        filters.append(f"match_date=lte.{date_to}")

    try:
        rows = supabase("historical_forecasts?" + "&".join(filters))
    except RuntimeError:
        return jsonify({"ok": False, "error": "missing_supabase_config"}), 500
    except urllib.error.HTTPError as exc:
        return jsonify({"ok": False, "error": f"supabase_http_{exc.code}"}), 502
    except (urllib.error.URLError, TimeoutError, ValueError):
        return jsonify({"ok": False, "error": "supabase_unavailable"}), 502

    selected = []
    for row in rows:
        probability = float(row.get(f"{market}_probability") or 0)
        if not (min_probability <= probability <= max_probability):
            continue
        actual = row.get("actual_outcome")
        selected.append({
            "match_date": row.get("match_date"),
            "league": row.get("league"),
            "home_team": row.get("home_team"),
            "away_team": row.get("away_team"),
            "market": market,
            "model_probability": probability,
            "actual_outcome": actual,
            "won": actual == market,
            "log_loss": float(row.get("log_loss") or 0),
            "brier_score": float(row.get("brier_score") or 0),
        })

    wins = sum(item["won"] for item in selected)
    monthly = defaultdict(lambda: {"selections": 0, "wins": 0, "probability_sum": 0.0})
    cumulative = []
    running_wins = 0
    for index, item in enumerate(selected, start=1):
        month = (item["match_date"] or "")[:7]
        monthly[month]["selections"] += 1
        monthly[month]["wins"] += int(item["won"])
        monthly[month]["probability_sum"] += item["model_probability"]
        running_wins += int(item["won"])
        cumulative.append({
            "index": index,
            "match_date": item["match_date"],
            "cumulative_wins": running_wins,
            "cumulative_hit_rate": running_wins / index,
        })

    monthly_summary = []
    for month, values in sorted(monthly.items()):
        monthly_summary.append({
            "month": month,
            "selections": values["selections"],
            "wins": values["wins"],
            "hit_rate": values["wins"] / values["selections"],
            "average_probability": values["probability_sum"] / values["selections"],
        })

    return jsonify({
        "ok": True,
        "status": "analysis_only",
        "filters": {
            "league": league or "all",
            "market": market,
            "date_from": date_from or None,
            "date_to": date_to or None,
            "min_probability": min_probability,
            "max_probability": max_probability,
        },
        "metrics": {
            "selections": len(selected),
            "wins": wins,
            "hit_rate": wins / len(selected) if selected else None,
            "average_probability": sum(i["model_probability"] for i in selected) / len(selected) if selected else None,
            "average_log_loss": sum(i["log_loss"] for i in selected) / len(selected) if selected else None,
            "average_brier_score": sum(i["brier_score"] for i in selected) / len(selected) if selected else None,
        },
        "monthly": monthly_summary,
        "equity_curve": cumulative,
        "selections": selected[-500:],
        "limitations": [
            "Historical forecast records do not yet include historical bookmaker odds.",
            "ROI, stake sizing and drawdown require an indexed historical odds dataset.",
            "This endpoint evaluates model calibration and selection frequency only.",
            "No bet, stake, accumulator, alert or trading recommendation is generated.",
        ],
    })


if __name__ == "__main__":
    app.run()

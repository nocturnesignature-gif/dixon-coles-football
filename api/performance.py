"""Performance endpoint with deduplicated prediction-quality metrics."""
import json
import os
import urllib.error
import urllib.request
from collections import defaultdict
from flask import Flask, jsonify

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


def aggregate(rows):
    settled = [r for r in rows if r.get("result") in ("won", "lost", "void")]
    won = sum(r.get("result") == "won" for r in settled)
    profit = sum(float(r["profit"]) for r in settled if r.get("profit") is not None)
    staked = sum(float(r.get("stake_amount") or 0) for r in settled)
    clv = [float(r["closing_line_value"]) for r in settled if r.get("closing_line_value") is not None]
    return {
        "bets": len(rows),
        "settled": len(settled),
        "won": won,
        "hit_rate": won / len(settled) if settled else None,
        "staked": staked,
        "profit": profit,
        "roi": profit / staked if staked else None,
        "average_clv": sum(clv) / len(clv) if clv else None,
    }


@app.after_request
def no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/performance")
def performance():
    try:
        bets = supabase("bet_history?select=*&order=created_at.desc&limit=1000")
        counts = supabase("football_quality_counts_v1?select=*")
        quality_counts = counts[0] if counts else {}
        overall = aggregate(bets)
        by_profile = defaultdict(list)
        by_strategy = defaultdict(list)
        by_month = defaultdict(list)
        for bet in bets:
            by_profile[bet.get("profile") or "unknown"].append(bet)
            by_strategy[bet.get("strategy") or bet.get("profile") or "unknown"].append(bet)
            month = (bet.get("created_at") or "")[:7]
            if month:
                by_month[month].append(bet)
        return jsonify({
            "ok": True,
            "overall": overall,
            "by_profile": {k: aggregate(v) for k, v in sorted(by_profile.items())},
            "by_strategy": {k: aggregate(v) for k, v in sorted(by_strategy.items())},
            "by_month": {k: aggregate(v) for k, v in sorted(by_month.items())},
            "prediction_quality": {
                "matches": quality_counts.get("distinct_matches", 0),
                "average_log_loss": quality_counts.get("log_loss"),
                "average_brier_score": quality_counts.get("brier_score"),
                "evaluated_forecasts": quality_counts.get("evaluated_forecasts", 0),
                "model_versions": quality_counts.get("model_versions", 0),
                "deduplication": "earliest_recorded_pre_kickoff_forecast_v1",
            },
            "limitations": [
                "Prediction quality uses one valid pre-kickoff forecast per distinct match.",
                "Betting metrics use the bet journal and are independent of prediction quality.",
                "No model validation or betting profitability is implied.",
            ],
        })
    except RuntimeError:
        return jsonify({"ok": False, "error": "missing_supabase_config"}), 500
    except urllib.error.HTTPError as exc:
        return jsonify({"ok": False, "error": f"supabase_http_{exc.code}"}), 502
    except (urllib.error.URLError, TimeoutError, ValueError):
        return jsonify({"ok": False, "error": "supabase_unavailable"}), 502


if __name__ == "__main__":
    app.run()

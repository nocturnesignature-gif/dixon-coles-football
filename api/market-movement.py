"""Read-only Pinnacle market-movement analysis; no betting recommendations."""
import json
import os
import urllib.error
import urllib.request
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


@app.after_request
def no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/market-movement")
def market_movement():
    try:
        forecasts = supabase("football_forecasts_v4?select=id,event_id,predicted_at,features&eligibility=eq.eligible&order=predicted_at.desc&limit=1000")
        events = supabase("football_events_v4?select=id,external_id,competition,home_team,away_team,kickoff_at&order=kickoff_at.desc&limit=1000")
        event_by_id = {e["id"]: e for e in events}
        movements = []
        for forecast in forecasts:
            event = event_by_id.get(forecast.get("event_id"))
            features = forecast.get("features") or {}
            start = features.get("prediction_odds_snapshot") or {}
            closing = features.get("closing_odds_snapshot") or {}
            start_odds = start.get("values") or {}
            closing_odds = closing.get("values") or {}
            if not event or not start_odds or not closing_odds:
                continue
            market_changes = {}
            for market in ("home", "draw", "away"):
                before = start_odds.get(market)
                after = closing_odds.get(market)
                if before and after and float(before) > 1 and float(after) > 1:
                    change = float(after) / float(before) - 1
                    market_changes[market] = {
                        "before": float(before),
                        "after": float(after),
                        "change": change,
                        "direction": "down" if change < -0.005 else "up" if change > 0.005 else "stable",
                    }
            if market_changes:
                movements.append({
                    "event_id": event.get("external_id"),
                    "competition": event.get("competition"),
                    "home_team": event.get("home_team"),
                    "away_team": event.get("away_team"),
                    "kickoff_at": event.get("kickoff_at"),
                    "predicted_at": forecast.get("predicted_at"),
                    "changes": market_changes,
                })
        return jsonify({
            "ok": True,
            "status": "analysis_only",
            "movements": movements,
            "limitations": [
                "Market movement is observational and does not prove smart money.",
                "No betting selection, stake, or accumulator is generated.",
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

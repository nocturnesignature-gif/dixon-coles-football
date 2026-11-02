"""Read-only CLV analytics; no betting recommendations."""
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


@app.after_request
def no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/clv-analytics")
def clv_analytics():
    try:
        forecasts = supabase("football_forecasts_v4?select=id,event_id,model_run_id,predicted_at,features,home_probability,draw_probability,away_probability&eligibility=eq.eligible&order=predicted_at.desc&limit=1000")
        events = supabase("football_events_v4?select=id,external_id,competition,home_team,away_team,kickoff_at&order=kickoff_at.desc&limit=1000")
        event_by_id = {e["id"]: e for e in events}
        rows = []
        for forecast in forecasts:
            event = event_by_id.get(forecast.get("event_id"))
            features = forecast.get("features") or {}
            snapshot = features.get("prediction_odds_snapshot") or {}
            prediction_odds = snapshot.get("values") or {}
            if not event or not prediction_odds:
                continue
            rows.append({
                "event_id": event.get("external_id"),
                "competition": event.get("competition"),
                "home_team": event.get("home_team"),
                "away_team": event.get("away_team"),
                "kickoff_at": event.get("kickoff_at"),
                "predicted_at": forecast.get("predicted_at"),
                "prediction_odds": prediction_odds,
                "closing_odds": features.get("closing_odds_snapshot"),
            })
        by_competition = defaultdict(list)
        for row in rows:
            by_competition[row["competition"]].append(row)
        summary = []
        for competition, items in sorted(by_competition.items()):
            clvs = []
            for item in items:
                closing = item.get("closing_odds") or {}
                for market in ("home", "draw", "away"):
                    taken = item["prediction_odds"].get(market)
                    closed = closing.get("values", {}).get(market)
                    if taken and closed and float(closed) > 1:
                        clvs.append(float(taken) / float(closed) - 1)
            summary.append({
                "competition": competition,
                "observations": len(clvs),
                "average_clv": sum(clvs) / len(clvs) if clvs else None,
                "positive_clv_rate": sum(c > 0 for c in clvs) / len(clvs) if clvs else None,
            })
        return jsonify({
            "ok": True,
            "status": "analysis_only",
            "rows": rows,
            "summary": summary,
            "limitations": [
                "CLV is an analytical measure and does not validate a model.",
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

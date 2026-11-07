"""Read-only CLV analytics based on historical opening and closing odds."""
import json
import os
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime
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


def parse_timestamp(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@app.after_request
def no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/clv-analytics")
def clv_analytics():
    try:
        forecasts = supabase(
            "football_forecasts_v4?select=id,event_id,model_run_id,predicted_at,features,home_probability,draw_probability,away_probability&eligibility=eq.eligible&order=predicted_at.desc&limit=1000"
        )
        events = supabase(
            "football_events_v4?select=id,external_id,competition,home_team,away_team,kickoff_at&order=kickoff_at.desc&limit=1000"
        )
        historical = supabase(
            "historical_match_odds?select=event_id,market,bookmaker,odds_type,odds,captured_at,kickoff_at&order=captured_at.asc&limit=10000"
        )

        event_by_id = {event["id"]: event for event in events}
        odds_by_event = defaultdict(lambda: defaultdict(list))
        for row in historical:
            event_id = row.get("event_id")
            odds_type = row.get("odds_type")
            market = row.get("market")
            if event_id and odds_type in {"prediction", "closing"} and market in {"home", "draw", "away"}:
                odds_by_event[event_id][odds_type].append(row)

        rows = []
        observations = []
        for forecast in forecasts:
            event = event_by_id.get(forecast.get("event_id"))
            if not event:
                continue
            event_id = event.get("external_id")
            opening = odds_by_event.get(event_id, {}).get("prediction", [])
            closing = odds_by_event.get(event_id, {}).get("closing", [])
            if not opening or not closing:
                continue

            predicted_at = parse_timestamp(forecast.get("predicted_at"))
            kickoff = parse_timestamp(event.get("kickoff_at"))
            eligible_openings = [
                row for row in opening
                if predicted_at and parse_timestamp(row.get("captured_at")) <= predicted_at
            ]
            eligible_closings = [
                row for row in closing
                if kickoff and parse_timestamp(row.get("captured_at")) <= kickoff
            ]
            if not eligible_openings or not eligible_closings:
                continue

            opening_by_market = {}
            for row in eligible_openings:
                opening_by_market[row["market"]] = row
            closing_by_market = {}
            for row in eligible_closings:
                closing_by_market[row["market"]] = row

            market_clvs = {}
            for market in ("home", "draw", "away"):
                taken = opening_by_market.get(market)
                closed = closing_by_market.get(market)
                if not taken or not closed:
                    continue
                taken_odds = float(taken["odds"])
                closed_odds = float(closed["odds"])
                if closed_odds <= 1:
                    continue
                clv = taken_odds / closed_odds - 1
                market_clvs[market] = {
                    "prediction_odds": taken_odds,
                    "closing_odds": closed_odds,
                    "clv": clv,
                    "bookmaker": taken.get("bookmaker"),
                    "prediction_captured_at": taken.get("captured_at"),
                    "closing_captured_at": closed.get("captured_at"),
                }
                observations.append({
                    "event_id": event_id,
                    "competition": event.get("competition"),
                    "home_team": event.get("home_team"),
                    "away_team": event.get("away_team"),
                    "kickoff_at": event.get("kickoff_at"),
                    "market": market,
                    **market_clvs[market],
                })

            if market_clvs:
                rows.append({
                    "event_id": event_id,
                    "competition": event.get("competition"),
                    "home_team": event.get("home_team"),
                    "away_team": event.get("away_team"),
                    "kickoff_at": event.get("kickoff_at"),
                    "predicted_at": forecast.get("predicted_at"),
                    "markets": market_clvs,
                })

        by_competition = defaultdict(list)
        for observation in observations:
            by_competition[observation["competition"]].append(observation["clv"])

        summary = []
        for competition, clvs in sorted(by_competition.items()):
            summary.append({
                "competition": competition,
                "observations": len(clvs),
                "average_clv": sum(clvs) / len(clvs) if clvs else None,
                "positive_clv_rate": sum(clv > 0 for clv in clvs) / len(clvs) if clvs else None,
            })

        return jsonify({
            "ok": True,
            "status": "analysis_only",
            "rows": rows,
            "observations": observations,
            "summary": summary,
            "limitations": [
                "CLV compares the captured prediction odds with the available closing odds.",
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

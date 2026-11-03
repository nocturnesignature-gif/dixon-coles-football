"""Capture pre-kickoff closing odds from the latest live odds observations."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify

app = Flask(__name__)


def request_json(path, method="GET", payload=None):
    base = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base or not key:
        raise RuntimeError("missing_supabase_config")
    req = urllib.request.Request(f"{base}/rest/v1/{path}", method=method, headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept": "application/json", "Prefer": "resolution=ignore-duplicates,return=representation"}, data=json.dumps(payload).encode() if payload is not None else None)
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@app.get("/api/capture-closing-odds")
def capture_closing_odds():
    try:
        now = datetime.now(timezone.utc)
        events = request_json("football_events_v4?select=id,external_id,source,active,competition,home_team,away_team,kickoff_at&order=kickoff_at.asc&limit=1000")
        events = [e for e in events if e.get("source") == "football_data_org" and e.get("active") and e.get("kickoff_at") and now <= parse(e["kickoff_at"]) <= now + timedelta(hours=2)]
        saved = 0
        for event in events:
            kickoff = parse(event["kickoff_at"])
            odds_rows = request_json(f"live_odds?select=event_id,home_team,away_team,market,odds,provider,captured_at&event_id=eq.{urllib.parse.quote(event['external_id'])}&order=captured_at.desc&limit=100")
            latest = {}
            for row in odds_rows:
                captured = parse(row["captured_at"])
                if captured > kickoff or (kickoff - captured).total_seconds() > 6 * 3600:
                    continue
                latest.setdefault(row["market"], row)
            history_rows = []
            for market, row in latest.items():
                history_rows.append({"match_date": event["kickoff_at"][:10], "league": event["competition"], "home_team": event["home_team"], "away_team": event["away_team"], "market": market, "bookmaker": row["provider"], "odds_type": "closing", "odds": row["odds"], "captured_at": row["captured_at"], "source": "live_odds", "event_id": event["external_id"], "kickoff_at": event["kickoff_at"]})
            if history_rows:
                request_json("historical_match_odds?on_conflict=match_date,league,home_team,away_team,market,bookmaker,odds_type", method="POST", payload=history_rows)
                saved += len(history_rows)
        return jsonify({"ok": True, "events_processed": len(events), "historical_closing_rows_saved": saved})
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return jsonify({"ok": False, "error": "closing_capture_failed", "details": str(exc)}), 500


if __name__ == "__main__":
    app.run()

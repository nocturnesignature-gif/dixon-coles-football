"""Capture prospective opening odds into live_odds and historical_match_odds."""
import json
import os
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, request

app = Flask(__name__)
SPORT_KEYS = {
    "Premier League": "soccer_epl",
    "EPL": "soccer_epl",
    "Primera Division": "soccer_spain_la_liga",
    "La Liga - Spain": "soccer_spain_la_liga",
    "Bundesliga": "soccer_germany_bundesliga",
    "Bundesliga - Germany": "soccer_germany_bundesliga",
    "Serie A": "soccer_italy_serie_a",
    "Ligue 1": "soccer_france_ligue_one",
    "Ligue 1 - France": "soccer_france_ligue_one",
}
STOP = {"fc", "cf", "ac", "sc", "as", "ss", "calcio", "1901", "de", "du", "des", "the"}


def request_json(path, method="GET", payload=None, extra_headers=None):
    base = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base or not key:
        raise RuntimeError("missing_supabase_config")
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept": "application/json", "Prefer": "resolution=ignore-duplicates,return=representation"}
    headers.update(extra_headers or {})
    req = urllib.request.Request(f"{base}/rest/v1/{path}", method=method, headers=headers, data=json.dumps(payload).encode() if payload is not None else None)
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def normalize(name):
    text = unicodedata.normalize("NFD", name or "")
    tokens = re.split(r"[^a-z0-9]+", "".join(c for c in text if not unicodedata.combining(c)).lower())
    return {t for t in tokens if t and t not in STOP}


def same_match(event, odds_event):
    home, away = normalize(event["home_team"]), normalize(event["away_team"])
    odds_home, odds_away = normalize(odds_event["home_team"]), normalize(odds_event["away_team"])
    home_score = len(home & odds_home) / max(1, len(home | odds_home))
    away_score = len(away & odds_away) / max(1, len(away | odds_away))
    kickoff = datetime.fromisoformat(event["kickoff_at"].replace("Z", "+00:00"))
    odds_kickoff = datetime.fromisoformat(odds_event["commence_time"].replace("Z", "+00:00"))
    return home_score >= 0.5 and away_score >= 0.5 and abs((kickoff - odds_kickoff).total_seconds()) <= 7200


@app.get("/api/sync-odds")
def sync_odds():
    api_key = os.environ.get("THE_ODDS_API_KEY", "")
    if not api_key:
        return jsonify({"ok": False, "error": "missing_the_odds_api_key"}), 500
    try:
        now = datetime.now(timezone.utc)
        events = request_json("football_events_v4?select=id,external_id,source,active,competition,home_team,away_team,kickoff_at&order=kickoff_at.asc&limit=1000")
        events = [e for e in events if e.get("source") == "football_data_org" and e.get("active") and e.get("kickoff_at") and now < datetime.fromisoformat(e["kickoff_at"].replace("Z", "+00:00")) < now + timedelta(days=7)]
        saved_live = saved_history = matched = 0
        requested = set()
        for event in events:
            sport_key = SPORT_KEYS.get(event["competition"])
            if not sport_key or sport_key in requested:
                continue
            requested.add(sport_key)
            params = urllib.parse.urlencode({"apiKey": api_key, "regions": "eu", "markets": "h2h", "oddsFormat": "decimal"})
            try:
                with urllib.request.urlopen(f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds?{params}", timeout=25) as response:
                    odds_events = json.load(response)
            except urllib.error.HTTPError as exc:
                return jsonify({"ok": False, "error": f"odds_api_http_{exc.code}", "sport_key": sport_key, "details": exc.read().decode(errors="replace")[:300]}), 502
            for odds_event in odds_events:
                if not same_match(event, odds_event):
                    continue
                matched += 1
                bookmaker = next(iter(odds_event.get("bookmakers", [])), None)
                market = next((m for m in bookmaker.get("markets", []) if m.get("key") == "h2h"), None) if bookmaker else None
                if not market:
                    continue
                prices = {o["name"]: o["price"] for o in market.get("outcomes", [])}
                captured_at = datetime.now(timezone.utc).isoformat()
                kickoff = event["kickoff_at"]
                live_rows, history_rows = [], []
                for market_name, outcome_name in (("home", odds_event["home_team"]), ("draw", "Draw"), ("away", odds_event["away_team"])):
                    price = prices.get(outcome_name)
                    if not price:
                        continue
                    live_rows.append({"event_id": event["external_id"], "home_team": event["home_team"], "away_team": event["away_team"], "market": market_name, "odds": price, "provider": bookmaker.get("key", "the_odds_api"), "captured_at": captured_at})
                    history_rows.append({"match_date": kickoff[:10], "league": event["competition"], "home_team": event["home_team"], "away_team": event["away_team"], "market": market_name, "bookmaker": bookmaker.get("key", "the_odds_api"), "odds_type": "prediction", "odds": price, "captured_at": captured_at, "source": "the_odds_api", "event_id": event["external_id"], "kickoff_at": kickoff})
                if live_rows:
                    request_json("live_odds", method="POST", payload=live_rows)
                    saved_live += len(live_rows)
                if history_rows:
                    request_json("historical_match_odds?on_conflict=match_date,league,home_team,away_team,market,bookmaker,odds_type", method="POST", payload=history_rows)
                    saved_history += len(history_rows)
        return jsonify({"ok": True, "events_processed": len(events), "events_matched": matched, "live_odds_saved": saved_live, "historical_prediction_rows_saved": saved_history})
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return jsonify({"ok": False, "error": "odds_sync_failed", "details": str(exc)}), 500


if __name__ == "__main__":
    app.run()

import json
import os
import re
import unicodedata
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify

app = Flask(__name__)

SPORT_KEYS = {
    "premier_league": "soccer_epl",
    "la_liga": "soccer_spain_la_liga",
    "bundesliga": "soccer_germany_bundesliga",
    "serie_a": "soccer_italy_serie_a",
    "ligue_1": "soccer_france_ligue_one",
}
STOP = {"fc", "cf", "ac", "sc", "as", "ss", "calcio", "1901", "de", "du", "des", "the"}


def normalize(name):
    text = unicodedata.normalize("NFD", name or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    tokens = re.split(r"[^a-z0-9]+", text.lower())
    return [t for t in tokens if t and t not in STOP]


def supabase_request(path, method="GET", payload=None):
    base_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base_url or not service_key:
        raise RuntimeError("missing_supabase_config")
    request = urllib.request.Request(
        f"{base_url}/rest/v1/{path}",
        method=method,
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        },
        data=json.dumps(payload).encode() if payload is not None else None,
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def same_match(event, odds_event):
    home = set(normalize(event["home_team"]))
    away = set(normalize(event["away_team"]))
    odds_home = set(normalize(odds_event["home_team"]))
    odds_away = set(normalize(odds_event["away_team"]))
    home_score = len(home & odds_home) / max(1, len(home | odds_home))
    away_score = len(away & odds_away) / max(1, len(away | odds_away))
    if home_score < 0.5 or away_score < 0.5:
        return False
    kickoff = datetime.fromisoformat(event["kickoff_at"].replace("Z", "+00:00"))
    odds_kickoff = datetime.fromisoformat(odds_event["commence_time"].replace("Z", "+00:00"))
    return abs((kickoff - odds_kickoff).total_seconds()) <= 7200


@app.get("/api/sync-odds")
def sync_odds():
    api_key = os.environ.get("THE_ODDS_API_KEY", "")
    if not api_key:
        return jsonify({"ok": False, "error": "missing_the_odds_api_key"}), 500
    try:
        now = datetime.now(timezone.utc)
        events = supabase_request(
            "football_events_v4?select=id,external_id,sport_key,competition,home_team,away_team,kickoff_at"
            "&source=eq.football_data_org&active=eq.true"
            f"&kickoff_at=gt.{urllib.parse.quote(now.isoformat())}"
            f"&kickoff_at=lt.{urllib.parse.quote((now+timedelta(days=7)).isoformat())}"
        )
        saved = 0
        matched = 0
        for event in events:
            sport_key = SPORT_KEYS.get(event["competition"])
            if not sport_key:
                continue
            params = urllib.parse.urlencode({
                "apiKey": api_key,
                "regions": "eu",
                "markets": "h2h",
                "oddsFormat": "decimal",
            })
            url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds?{params}"
            with urllib.request.urlopen(url, timeout=20) as response:
                odds_events = json.load(response)
            match = next((o for o in odds_events if same_match(event, o)), None)
            if not match:
                continue
            matched += 1
            bookmaker = next(iter(match.get("bookmakers", [])), None)
            if not bookmaker:
                continue
            market = next((m for m in bookmaker.get("markets", []) if m.get("key") == "h2h"), None)
            if not market:
                continue
            prices = {o["name"]: o["price"] for o in market.get("outcomes", [])}
            rows = []
            for market_name, outcome_name in (("home", event["home_team"]), ("draw", "Draw"), ("away", event["away_team"])):
                price = prices.get(outcome_name)
                if price:
                    rows.append({
                        "event_id": event["external_id"],
                        "home_team": event["home_team"],
                        "away_team": event["away_team"],
                        "market": market_name,
                        "odds": price,
                        "provider": bookmaker.get("key", "the_odds_api"),
                    })
            if rows:
                supabase_request("live_odds", method="POST", payload=rows)
                saved += len(rows)
        return jsonify({"ok": True, "events_processed": len(events), "events_matched": matched, "odds_saved": saved})
    except urllib.error.HTTPError as exc:
        return jsonify({"ok": False, "error": f"odds_api_http_{exc.code}"}), 502
    except (RuntimeError, urllib.error.URLError) as exc:
        return jsonify({"ok": False, "error": "odds_sync_failed"}), 500

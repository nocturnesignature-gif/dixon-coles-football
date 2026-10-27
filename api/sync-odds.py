import json
import os
import re
import unicodedata
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, request

app = Flask(__name__)

SPORT_KEYS = {
    "Premier League": "soccer_epl",
    "Primera Division": "soccer_spain_la_liga",
    "Bundesliga": "soccer_germany_bundesliga",
    "Serie A": "soccer_italy_serie_a",
    "Ligue 1": "soccer_france_ligue_one",
}
STOP = {"fc", "cf", "ac", "sc", "as", "ss", "calcio", "1901", "de", "du", "des", "the"}
DOUBLE_CHANCE_NAMES = {
    "Home/Draw": "1X",
    "Home/Away": "12",
    "Draw/Away": "X2",
}


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
    api_request = urllib.request.Request(
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
    with urllib.request.urlopen(api_request, timeout=20) as response:
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


def extract_prices(match, market_key):
    bookmaker = next(iter(match.get("bookmakers", [])), None)
    if not bookmaker:
        return None, None
    market = next((m for m in bookmaker.get("markets", []) if m.get("key") == market_key), None)
    if not market:
        return None, None
    prices = {o["name"]: o["price"] for o in market.get("outcomes", [])}
    return prices, bookmaker.get("key", "the_odds_api")


@app.get("/api/sync-odds")
def sync_odds():
    api_key = os.environ.get("THE_ODDS_API_KEY", "")
    if not api_key:
        return jsonify({"ok": False, "error": "missing_the_odds_api_key"}), 500
    debug = request.args.get("debug") == "true"
    try:
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(days=7)
        all_events = supabase_request(
            "football_events_v4?select=id,external_id,source,active,competition,home_team,away_team,kickoff_at&order=kickoff_at.asc&limit=1000"
        )
        source_events = [e for e in all_events if e.get("source") == "football_data_org"]
        active_events = [e for e in source_events if e.get("active") is True]
        future_events = [e for e in active_events if datetime.fromisoformat(e["kickoff_at"].replace("Z", "+00:00")) > now]
        events = [e for e in future_events if datetime.fromisoformat(e["kickoff_at"].replace("Z", "+00:00")) < horizon]
        if debug:
            return jsonify({
                "ok": True,
                "counts": {
                    "all": len(all_events),
                    "source": len(source_events),
                    "active": len(active_events),
                    "future": len(future_events),
                    "next_7_days": len(events),
                    "supported_competitions": len([e for e in events if e.get("competition") in SPORT_KEYS]),
                },
                "sample_events": [
                    {k: e.get(k) for k in ("source", "active", "competition", "home_team", "away_team", "kickoff_at")}
                    for e in all_events[:5]
                ],
            })
        saved = 0
        matched = 0
        for event in events:
            sport_key = SPORT_KEYS.get(event["competition"])
            if not sport_key:
                continue
            params = urllib.parse.urlencode({
                "apiKey": api_key,
                "regions": "eu",
                "markets": "h2h,double_chance",
                "oddsFormat": "decimal",
            })
            url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds?{params}"
            with urllib.request.urlopen(url, timeout=20) as response:
                odds_events = json.load(response)
            match = next((o for o in odds_events if same_match(event, o)), None)
            if not match:
                continue
            matched += 1
            rows = []
            h2h_prices, provider = extract_prices(match, "h2h")
            if h2h_prices:
                for market_name, outcome_name in (("home", event["home_team"]), ("draw", "Draw"), ("away", event["away_team"])):
                    price = h2h_prices.get(outcome_name)
                    if price:
                        rows.append({
                            "event_id": event["external_id"],
                            "home_team": event["home_team"],
                            "away_team": event["away_team"],
                            "market": market_name,
                            "odds": price,
                            "provider": provider,
                        })
            dc_prices, dc_provider = extract_prices(match, "double_chance")
            if dc_prices:
                for outcome_name, market_name in DOUBLE_CHANCE_NAMES.items():
                    price = dc_prices.get(outcome_name)
                    if price:
                        rows.append({
                            "event_id": event["external_id"],
                            "home_team": event["home_team"],
                            "away_team": event["away_team"],
                            "market": market_name,
                            "odds": price,
                            "provider": dc_provider,
                        })
            if rows:
                supabase_request("live_odds", method="POST", payload=rows)
                saved += len(rows)
        return jsonify({"ok": True, "events_processed": len(events), "events_matched": matched, "odds_saved": saved})
    except urllib.error.HTTPError as exc:
        return jsonify({"ok": False, "error": f"odds_api_http_{exc.code}"}), 502
    except (RuntimeError, urllib.error.URLError) as exc:
        return jsonify({"ok": False, "error": "odds_sync_failed"}), 500

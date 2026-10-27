import json
import os
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


@app.get("/api/sync-odds")
def sync_odds():
    api_key = os.environ.get("THE_ODDS_API_KEY", "")
    if not api_key:
        return jsonify({"ok": False, "error": "missing_the_odds_api_key"}), 500
    try:
        now = datetime.now(timezone.utc)
        events = supabase_request(
            "football_events_v4?select=id,external_id,sport_key,competition,home_team,away_team"
            "&source=eq.football_data_org&active=eq.true"
            f"&kickoff_at=gt.{urllib.parse.quote(now.isoformat())}"
            f"&kickoff_at=lt.{urllib.parse.quote((now+timedelta(days=7)).isoformat())}"
        )
        saved = 0
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
            match = next((o for o in odds_events if o.get("id") == event.get("external_id")), None)
            if not match:
                continue
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
        return jsonify({"ok": True, "events_processed": len(events), "odds_saved": saved})
    except urllib.error.HTTPError as exc:
        return jsonify({"ok": False, "error": f"odds_api_http_{exc.code}"}), 502
    except (RuntimeError, urllib.error.URLError) as exc:
        return jsonify({"ok": False, "error": "odds_sync_failed"}), 500

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from flask import Flask, jsonify, request

app = Flask(__name__)

PINNACLE = "pinnacle"
VALID_MARKETS = {"home", "draw", "away"}
VALID_ODDS_TYPES = {"prediction", "closing"}


def require_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing_env_{name}")
    return value


def supabase_rpc(function_name, payload):
    base = require_env("SUPABASE_URL").rstrip("/")
    key = require_env("SUPABASE_SERVICE_ROLE_KEY")
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/rest/v1/rpc/{function_name}",
        data=body,
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def normalize_market(value):
    market = str(value or "").strip().lower()
    if market not in VALID_MARKETS:
        raise ValueError(f"invalid_market_{market}")
    return market


def normalize_odds(value):
    try:
        odds = float(value)
    except (TypeError, ValueError):
        raise ValueError("invalid_odds")
    if not odds > 1:
        raise ValueError("invalid_odds")
    return odds


def normalize_timestamp(value):
    if not value:
        raise ValueError("missing_timestamp")
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        ).astimezone(timezone.utc).isoformat()
    except ValueError:
        raise ValueError("invalid_timestamp")


def normalize_match_date(value):
    return normalize_timestamp(value)[:10]


def extract_pinnacle_rows(payload, odds_type, source):
    if not isinstance(payload, list):
        raise ValueError("invalid_payload")

    rows = []

    for event in payload:
        event_id = str(event.get("id") or "").strip()
        if not event_id:
            raise ValueError("missing_event_id")

        kickoff_at = normalize_timestamp(event.get("commence_time"))
        match_date = normalize_match_date(event.get("commence_time"))
        league = str(event.get("sport_title") or event.get("sport_key") or "").strip()
        home_team = str(event.get("home_team") or "").strip()
        away_team = str(event.get("away_team") or "").strip()

        if not league or not home_team or not away_team:
            raise ValueError("missing_event_context")

        pinnacle = next(
            (
                bookmaker
                for bookmaker in event.get("bookmakers") or []
                if bookmaker.get("key") == PINNACLE
            ),
            None,
        )

        if not pinnacle:
            continue

        h2h = next(
            (
                market
                for market in pinnacle.get("markets") or []
                if market.get("key") == "h2h"
            ),
            None,
        )

        if not h2h:
            continue

        captured_at = normalize_timestamp(h2h.get("last_update"))

        for outcome in h2h.get("outcomes") or []:
            market = normalize_market(outcome.get("name"))
            odds = normalize_odds(outcome.get("price"))

            rows.append({
                "event_id": event_id,
                "bookmaker": PINNACLE,
                "odds_type": odds_type,
                "market": market,
                "captured_at": captured_at,
                "odds": odds,
                "match_date": match_date,
                "league": league,
                "home_team": home_team,
                "away_team": away_team,
                "kickoff_at": kickoff_at,
                "source": source,
            })

    return rows


@app.post("/api/collect-the-odds-api-pinnacle")
def collect_the_odds_api_pinnacle():
    try:
        api_key = require_env("THE_ODDS_API_KEY")
        sport = require_env("THE_ODDS_API_SPORT")
        odds_type = request.args.get("odds_type", "prediction").strip().lower()

        if odds_type not in VALID_ODDS_TYPES:
            raise ValueError("invalid_odds_type")

        query = urllib.parse.urlencode({
            "apiKey": api_key,
            "regions": "eu",
            "markets": "h2h",
            "bookmakers": PINNACLE,
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        })

        url = f"https://api.the-odds-api.com/v4/sports/{sport}/odds/?{query}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})

        with urllib.request.urlopen(req, timeout=30) as response:
            payload = json.load(response)

        source = f"the-odds-api:pinnacle:{sport}:{odds_type}"
        rows = extract_pinnacle_rows(payload, odds_type, source)
        inserted = 0

        for row in rows:
            supabase_rpc(
                "insert_historical_odds_versioned_traced",
                {
                    "p_event_id": row["event_id"],
                    "p_bookmaker": row["bookmaker"],
                    "p_odds_type": row["odds_type"],
                    "p_market": row["market"],
                    "p_captured_at": row["captured_at"],
                    "p_odds": row["odds"],
                    "p_match_date": row["match_date"],
                    "p_league": row["league"],
                    "p_home_team": row["home_team"],
                    "p_away_team": row["away_team"],
                    "p_kickoff_at": row["kickoff_at"],
                    "p_source": row["source"],
                },
            )
            inserted += 1

        return jsonify({
            "ok": True,
            "status": "collection_only",
            "provider": "the-odds-api",
            "bookmaker": PINNACLE,
            "sport": sport,
            "odds_type": odds_type,
            "ingested": inserted,
            "limitations": [
                "No candidate, position, bankroll change or betting recommendation is created.",
                "generate_paper_candidates_v1 remains inactive.",
            ],
        })

    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception:
        return jsonify({"ok": False, "error": "collection_failed"}), 502


if __name__ == "__main__":
    app.run()

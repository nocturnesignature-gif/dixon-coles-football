"""Consolidated Vercel API entrypoint for the football forecasting application."""

import json
import math
import os
import re
import unicodedata
import urllib.parse
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from itertools import combinations
from flask import Flask, jsonify, request

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.calibration import calibrate_1x2

app = Flask(__name__)

LEAGUES = ["ligue_1", "premier_league", "la_liga", "bundesliga", "serie_a"]
LEAGUE_EVENT_ALIASES = {
    "ligue_1": ["Ligue 1"],
    "premier_league": ["Premier League"],
    "la_liga": ["Primera Division", "LaLiga"],
    "bundesliga": ["Bundesliga"],
    "serie_a": ["Serie A"],
}
RESULTS_VIEW = "football_prospective_verified_scores_v1"
MIN_ODDS = 1.45
MAX_KELLY_FRACTION = 0.01
MAX_DAILY_EXPOSURE_FRACTION = 0.05
MAX_EXPECTED_VALUE = 0.15
MIN_PLAUSIBLE_PROBABILITY = 0.02
MAX_PLAUSIBLE_PROBABILITY = 0.85
MIN_HISTORY_FOR_FITTING = 50
MAX_ODDS_AGE_HOURS = 6

SPORT_KEYS = {
    "Premier League": "soccer_epl",
    "Primera Division": "soccer_spain_la_liga",
    "Bundesliga": "soccer_germany_bundesliga",
    "Serie A": "soccer_italy_serie_a",
    "Ligue 1": "soccer_france_ligue_one",
}
STOP = {"fc", "cf", "ac", "sc", "as", "ss", "calcio", "1901", "de", "du", "des", "the"}

PROFILES = {
    "prudent": {"label": "Prudent", "minimum_single_probability": 0.65, "minimum_expected_value": 0.03, "maximum_odds": 2.00},
    "balanced": {"label": "Équilibré", "minimum_single_probability": 0.55, "minimum_expected_value": 0.05, "maximum_odds": 3.80},
    "value": {"label": "Valeur", "minimum_single_probability": 0.45, "minimum_expected_value": 0.10, "maximum_odds": 5.00},
}


class SupabaseError(Exception):
    def __init__(self, status, body):
        super().__init__(f"Supabase HTTP {status}")
        self.status = status
        self.body = body


def supabase_request(path, method="GET", payload=None):
    base_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base_url or not service_key:
        raise RuntimeError("missing_supabase_config")
    req = urllib.request.Request(
        f"{base_url}/rest/v1/{path}",
        method=method,
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Prefer": "return=representation",
        },
        data=json.dumps(payload).encode() if payload is not None else None,
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise SupabaseError(exc.code, exc.read().decode(errors="replace")[:500]) from exc


def parse_datetime(value):
    return datetime.fromisoformat((value or "").replace("Z", "+00:00"))


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def kelly_fraction(expected_value, odds):
    if odds <= 1 or expected_value <= 0:
        return 0
    return min(MAX_KELLY_FRACTION, 0.25 * expected_value / (odds - 1))


def confidence(probability):
    if probability >= 0.65:
        return "Élevée"
    if probability >= 0.55:
        return "Moyenne"
    return "Faible"


def implausible(forecast):
    values = [forecast["home_probability"], forecast["draw_probability"], forecast["away_probability"]]
    return any(v is None or v < MIN_PLAUSIBLE_PROBABILITY or v > MAX_PLAUSIBLE_PROBABILITY for v in values)


def aggregate(rows):
    settled = [r for r in rows if r.get("result") in ("won", "lost", "void")]
    won = sum(r.get("result") == "won" for r in settled)
    profit = sum(float(r["profit"]) for r in settled if r.get("profit") is not None)
    staked = sum(float(r.get("stake_amount") or 0) for r in settled)
    clv = [float(r["closing_line_value"]) for r in settled if r.get("closing_line_value") is not None]
    return {
        "bets": len(rows), "settled": len(settled), "won": won,
        "hit_rate": ratio(won, len(settled)), "staked": staked, "profit": profit,
        "roi": ratio(profit, staked), "average_clv": ratio(sum(clv), len(clv)),
    }


def prediction_metrics(rows):
    log_losses, brier_scores = [], []
    for row in rows:
        outcome = row.get("actual_outcome")
        probs = {"home": row.get("home_probability"), "draw": row.get("draw_probability"), "away": row.get("away_probability")}
        if outcome not in probs or any(probs[k] is None for k in probs):
            continue
        probs = {k: max(min(float(v), 0.999999), 0.000001) for k, v in probs.items()}
        log_losses.append(-math.log(probs[outcome]))
        brier_scores.append(sum((probs[k] - (1 if k == outcome else 0)) ** 2 for k in probs))
    return {"matches": len(log_losses), "average_log_loss": ratio(sum(log_losses), len(log_losses)), "average_brier_score": ratio(sum(brier_scores), len(brier_scores))}


def normalize(name):
    text = unicodedata.normalize("NFD", name or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    tokens = re.split(r"[^a-z0-9]+", text.lower())
    return [t for t in tokens if t and t not in STOP]


def same_match(event, odds_event):
    home = set(normalize(event["home_team"]))
    away = set(normalize(event["away_team"]))
    odds_home = set(normalize(odds_event["home_team"]))
    odds_away = set(normalize(odds_event["away_team"]))
    home_score = len(home & odds_home) / max(1, len(home | odds_home))
    away_score = len(away & odds_away) / max(1, len(away | odds_away))
    if home_score < 0.5 or away_score < 0.5:
        return False
    kickoff = parse_datetime(event["kickoff_at"])
    odds_kickoff = parse_datetime(odds_event["commence_time"])
    return abs((kickoff - odds_kickoff).total_seconds()) <= 7200


def extract_prices(match, market_key):
    bookmaker = next(iter(match.get("bookmakers", [])), None)
    if not bookmaker:
        return None, None
    market = next((m for m in bookmaker.get("markets", []) if m.get("key") == market_key), None)
    if not market:
        return None, None
    return {o["name"]: o["price"] for o in market.get("outcomes", [])}, bookmaker.get("key", "the_odds_api")


def fresh_odds(odd, kickoff):
    captured = parse_datetime(odd.get("captured_at"))
    return captured <= kickoff and (kickoff - captured).total_seconds() <= MAX_ODDS_AGE_HOURS * 3600


@app.get("/api/forecasts")
def forecasts():
    profile_key = request.args.get("profile", "balanced").lower()
    if profile_key not in PROFILES:
        return jsonify({"ok": False, "error": "invalid_profile"}), 400
    profile = PROFILES[profile_key]
    try:
        runs = supabase_request("football_model_runs_v4?select=id,model_version,competition,status,created_at&competition=in.(" + ",".join(LEAGUES) + ")&order=created_at.desc&limit=100")
        latest = {}
        for run in runs:
            latest.setdefault(run["competition"], run)
        versions = [run["model_version"] for run in latest.values()]
        rows = supabase_request("football_experimental_forecasts_v4?select=*&model_version=in.(" + ",".join(versions) + ")&order=kickoff_at.asc") if versions else []
        odds_rows = supabase_request("live_odds?select=event_id,market,odds,provider,captured_at&order=captured_at.desc&limit=1000")
        latest_odds = {}
        for row in odds_rows:
            latest_odds.setdefault((row["event_id"], row["market"]), row)
        try:
            history = supabase_request(f"{RESULTS_VIEW}?select=home_probability,draw_probability,away_probability,actual_outcome&order=kickoff_at.desc&limit=500")
        except SupabaseError:
            history = []
        by_version, bets = {}, []
        for forecast in rows:
            by_version.setdefault(forecast["model_version"], []).append(forecast)
            event_id = forecast.get("external_id")
            kickoff = parse_datetime(forecast.get("kickoff_at"))
            probabilities = calibrate_1x2(forecast["home_probability"], forecast["draw_probability"], forecast["away_probability"], history)
            forecast.update({"calibrated_home_probability": probabilities["home"], "calibrated_draw_probability": probabilities["draw"], "calibrated_away_probability": probabilities["away"], "quality_flag": "implausible_raw_probabilities" if implausible(forecast) else "ok"})
            if forecast["quality_flag"] != "ok":
                continue
            for market, outcome in (("home", "Domicile"), ("draw", "Nul"), ("away", "Extérieur")):
                odd = latest_odds.get((event_id, market))
                if not odd or not fresh_odds(odd, kickoff):
                    continue
                probability = probabilities[market]
                expected_value = probability * odd["odds"] - 1
                if not (expected_value >= profile["minimum_expected_value"] and expected_value <= MAX_EXPECTED_VALUE and MIN_ODDS <= odd["odds"] <= profile["maximum_odds"] and probability >= profile["minimum_single_probability"]):
                    continue
                bets.append({"event_id": event_id, "competition": forecast["competition"], "home_team": forecast["home_team"], "away_team": forecast["away_team"], "kickoff_at": forecast["kickoff_at"], "market": market, "outcome": outcome, "model_probability": probability, "raw_model_probability": forecast[f"{market}_probability"], "odds": odd["odds"], "expected_value": expected_value, "confidence": confidence(probability), "suggested_stake_fraction": kelly_fraction(expected_value, odd["odds"]), "provider": odd["provider"], "captured_at": odd["captured_at"], "odds_age_hours": round((kickoff - parse_datetime(odd["captured_at"])).total_seconds() / 3600, 2)})
        bets.sort(key=lambda b: b["expected_value"] * b["model_probability"], reverse=True)
        leagues = []
        for league in LEAGUES:
            run = latest.get(league)
            if not run:
                continue
            aliases = LEAGUE_EVENT_ALIASES.get(league, [])
            league_forecasts = [f for f in by_version.get(run["model_version"], []) if f.get("competition") in aliases]
            leagues.append({"competition": league, "event_competitions": aliases, "model": {"id": run["id"], "version": run["model_version"], "status": run["status"]}, "forecasts": league_forecasts})
        results = supabase_request(f"{RESULTS_VIEW}?select=*&order=kickoff_at.asc")
        return jsonify({"ok": True, "status": "experimental_unvalidated", "profile": profile_key, "profile_label": profile["label"], "calibration": {"method": "per_outcome_logistic" if len(history) >= MIN_HISTORY_FOR_FITTING else "legacy_high_probability_shrinkage", "history_rows": len(history), "maximum_expected_value": MAX_EXPECTED_VALUE, "plausible_probability_range": [MIN_PLAUSIBLE_PROBABILITY, MAX_PLAUSIBLE_PROBABILITY]}, "risk_controls": {"maximum_kelly_fraction": MAX_KELLY_FRACTION, "maximum_daily_exposure_fraction": MAX_DAILY_EXPOSURE_FRACTION, "maximum_odds_age_hours": MAX_ODDS_AGE_HOURS}, "leagues": leagues, "bets": bets, "double_chance": [], "combos": [], "results": results})
    except SupabaseError as exc:
        return jsonify({"ok": False, "error": f"supabase_http_{exc.status}", "details": exc.body}), 502
    except (RuntimeError, urllib.error.URLError) as exc:
        return jsonify({"ok": False, "error": "configuration_or_upstream_error"}), 500


@app.get("/api/bets")
def list_bets():
    try:
        bets = supabase_request("bet_history?select=*&order=created_at.desc&limit=200")
        return jsonify({"ok": True, "bets": bets, "summary": aggregate(bets)})
    except SupabaseError as exc:
        return jsonify({"ok": False, "error": f"supabase_http_{exc.status}", "details": exc.body}), 502
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/bets")
def create_bet():
    payload = request.get_json(silent=True) or {}
    required = ["home_team", "away_team", "market", "outcome", "model_probability", "placed_odds", "expected_value", "stake_fraction"]
    missing = [field for field in required if payload.get(field) is None]
    if missing:
        return jsonify({"ok": False, "error": f"missing_fields:{','.join(missing)}"}), 400
    try:
        row = {"event_id": payload.get("event_id"), "competition": payload.get("competition"), "home_team": payload["home_team"], "away_team": payload["away_team"], "kickoff_at": payload.get("kickoff_at"), "market": payload["market"], "outcome": payload["outcome"], "model_probability": payload["model_probability"], "placed_odds": payload["placed_odds"], "expected_value": payload["expected_value"], "stake_fraction": payload["stake_fraction"], "stake_amount": payload.get("stake_amount"), "currency": payload.get("currency", "EUR"), "profile": payload.get("profile", "balanced"), "result": "pending"}
        created = supabase_request("bet_history", method="POST", payload=row)
        return jsonify({"ok": True, "bet": created[0] if created else row}), 201
    except SupabaseError as exc:
        return jsonify({"ok": False, "error": f"supabase_http_{exc.status}", "details": exc.body}), 502
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/bets/<bet_id>/settle")
def settle_bet(bet_id):
    payload = request.get_json(silent=True) or {}
    result = payload.get("result")
    if result not in ("won", "lost", "void"):
        return jsonify({"ok": False, "error": "invalid_result"}), 400
    try:
        existing = supabase_request(f"bet_history?id=eq.{bet_id}&select=*")
        if not existing:
            return jsonify({"ok": False, "error": "bet_not_found"}), 404
        bet = existing[0]
        closing_odds = payload.get("closing_odds")
        clv = float(bet["placed_odds"]) / closing_odds - 1 if closing_odds and closing_odds > 1 else None
        stake = float(bet.get("stake_amount") or 0)
        profit = stake * (float(bet["placed_odds"]) - 1) if result == "won" else -stake if result == "lost" else 0
        updated = supabase_request(f"bet_history?id=eq.{bet_id}", method="PATCH", payload={"result": result, "closing_odds": closing_odds, "closing_line_value": clv, "profit": profit, "settled_at": datetime.now(timezone.utc).isoformat()})
        return jsonify({"ok": True, "bet": updated[0] if updated else None})
    except SupabaseError as exc:
        return jsonify({"ok": False, "error": f"supabase_http_{exc.status}", "details": exc.body}), 502
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/performance")
def performance():
    try:
        bets = supabase_request("bet_history?select=*&order=created_at.desc&limit=1000")
        overall = aggregate(bets)
        by_profile, by_strategy, by_month = defaultdict(list), defaultdict(list), defaultdict(list)
        for bet in bets:
            by_profile[bet.get("profile") or "unknown"].append(bet)
            by_strategy[bet.get("strategy") or bet.get("profile") or "unknown"].append(bet)
            month = (bet.get("created_at") or "")[:7]
            if month:
                by_month[month].append(bet)
        try:
            results = supabase_request(f"{RESULTS_VIEW}?select=home_probability,draw_probability,away_probability,actual_outcome&order=kickoff_at.desc&limit=1000")
            quality = prediction_metrics(results)
        except SupabaseError:
            quality = {"matches": 0, "average_log_loss": None, "average_brier_score": None}
        return jsonify({"ok": True, "overall": overall, "by_profile": {k: aggregate(v) for k, v in sorted(by_profile.items())}, "by_strategy": {k: aggregate(v) for k, v in sorted(by_strategy.items())}, "by_month": {k: aggregate(v) for k, v in sorted(by_month.items())}, "prediction_quality": quality})
    except SupabaseError as exc:
        return jsonify({"ok": False, "error": f"supabase_http_{exc.status}", "details": exc.body}), 502
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/sync-odds")
def sync_odds():
    api_key = os.environ.get("THE_ODDS_API_KEY", "")
    if not api_key:
        return jsonify({"ok": False, "error": "missing_the_odds_api_key"}), 500
    debug = request.args.get("debug") == "true"
    try:
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(days=7)
        all_events = supabase_request("football_events_v4?select=id,external_id,source,active,competition,home_team,away_team,kickoff_at&order=kickoff_at.asc&limit=1000")
        source_events = [e for e in all_events if e.get("source") == "football_data_org"]
        active_events = [e for e in source_events if e.get("active") is True]
        future_events = [e for e in active_events if parse_datetime(e["kickoff_at"]) > now]
        events = [e for e in future_events if parse_datetime(e["kickoff_at"]) < horizon]
        if debug:
            return jsonify({"ok": True, "counts": {"all": len(all_events), "source": len(source_events), "active": len(active_events), "future": len(future_events), "next_7_days": len(events), "supported_competitions": len([e for e in events if e.get("competition") in SPORT_KEYS])}, "sample_events": [{k: e.get(k) for k in ("source", "active", "competition", "home_team", "away_team", "kickoff_at")} for e in all_events[:5]]})
        saved = matched = 0
        requested_sports = set()
        for event in events:
            sport_key = SPORT_KEYS.get(event["competition"])
            if not sport_key or sport_key in requested_sports:
                continue
            requested_sports.add(sport_key)
            params = urllib.parse.urlencode({"apiKey": api_key, "regions": "eu", "markets": "h2h", "oddsFormat": "decimal"})
            url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds?{params}"
            try:
                with urllib.request.urlopen(url, timeout=20) as response:
                    odds_events = json.load(response)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:300]
                return jsonify({"ok": False, "error": f"odds_api_http_{exc.code}", "sport_key": sport_key, "details": detail}), 502
            for odds_event in odds_events:
                event = next((e for e in events if SPORT_KEYS.get(e["competition"]) == sport_key and same_match(e, odds_event)), None)
                if not event:
                    continue
                matched += 1
                prices, provider = extract_prices(odds_event, "h2h")
                rows = []
                if prices:
                    captured_at = datetime.now(timezone.utc).isoformat()
                    for market_name, outcome_name in (("home", odds_event["home_team"]), ("draw", "Draw"), ("away", odds_event["away_team"])):
                        price = prices.get(outcome_name)
                        if price:
                            rows.append({"event_id": event["external_id"], "home_team": event["home_team"], "away_team": event["away_team"], "market": market_name, "odds": price, "provider": provider, "captured_at": captured_at})
                if rows:
                    supabase_request("live_odds", method="POST", payload=rows)
                    saved += len(rows)
        return jsonify({"ok": True, "events_processed": len(events), "events_matched": matched, "odds_saved": saved})
    except urllib.error.HTTPError as exc:
        return jsonify({"ok": False, "error": f"odds_api_http_{exc.code}"}), 502
    except (RuntimeError, urllib.error.URLError) as exc:
        return jsonify({"ok": False, "error": "odds_sync_failed"}), 500


@app.get("/api/capture-closing-odds")
def capture_closing_odds():
    try:
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(minutes=20)
        pending = supabase_request("bet_history?select=id,event_id,market,placed_odds,kickoff_at&result=eq.pending&kickoff_at=gte." + now.isoformat() + "&kickoff_at=lt." + horizon.isoformat())
        if not pending:
            return jsonify({"ok": True, "bets_processed": 0, "closing_odds_captured": 0})
        odds = supabase_request("live_odds?select=event_id,market,odds,captured_at&order=captured_at.desc&limit=1000")
        latest = {}
        for row in odds:
            latest.setdefault((row["event_id"], row["market"]), row)
        captured = 0
        for bet in pending:
            reference = latest.get((bet.get("event_id"), bet["market"]))
            if not reference:
                continue
            closing_odds = reference["odds"]
            clv = float(bet["placed_odds"]) / closing_odds - 1 if closing_odds > 1 else None
            supabase_request(f"bet_history?id=eq.{bet['id']}", method="PATCH", payload={"closing_odds": closing_odds, "closing_line_value": clv})
            captured += 1
        return jsonify({"ok": True, "bets_processed": len(pending), "closing_odds_captured": captured})
    except SupabaseError as exc:
        return jsonify({"ok": False, "error": f"supabase_http_{exc.status}", "details": exc.body}), 502
    except (RuntimeError, urllib.error.URLError) as exc:
        return jsonify({"ok": False, "error": "closing_odds_capture_failed"}), 500


@app.get("/api/check-alerts")
def check_alerts():
    try:
        return jsonify({"ok": True, "alerts_sent": 0, "reason": "alerts_temporarily_disabled_during_calibration"})
    except Exception:
        return jsonify({"ok": False, "error": "alert_check_failed"}), 500


@app.get("/api/collect-results")
def collect_results():
    try:
        service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        if not service_key:
            return jsonify({"ok": False, "error": "missing_service_key"}), 500
        base_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
        req = urllib.request.Request(
            f"{base_url}/functions/v1/football-data-status-ingest-v4",
            method="POST",
            headers={"Authorization": f"Bearer {service_key}", "Content-Type": "application/json"},
            data=json.dumps({"days": 2, "dry_run": False}).encode(),
        )
        with urllib.request.urlopen(req, timeout=60) as response:
            body = response.read()
        return app.response_class(body, status=response.status, mimetype="application/json")
    except urllib.error.HTTPError as exc:
        return jsonify({"ok": False, "error": f"upstream_http_{exc.code}", "details": exc.read().decode(errors="replace")[:500]}), 502
    except (RuntimeError, urllib.error.URLError) as exc:
        return jsonify({"ok": False, "error": "collection_failed"}), 500


@app.get("/api/diagnostics")
def diagnostics():
    try:
        now = datetime.now(timezone.utc)
        forecasts = supabase_request("football_experimental_forecasts_v4?select=external_id,competition,home_team,away_team,kickoff_at,predicted_at,home_probability,draw_probability,away_probability,eligibility,model_version&order=kickoff_at.asc&limit=200")
        future = [f for f in forecasts if parse_datetime(f["kickoff_at"]) > now]
        past = [f for f in forecasts if parse_datetime(f["kickoff_at"]) <= now]
        try:
            verified = supabase_request(f"{RESULTS_VIEW}?select=forecast_id,competition,home_team,away_team,kickoff_at,predicted_at,recorded_at,kickoff_at_snapshot,actual_outcome,home_score,away_score,log_loss,brier_score&order=kickoff_at.desc&limit=200")
        except SupabaseError as exc:
            verified = {"error": f"supabase_http_{exc.status}", "details": exc.body}
        by_competition = defaultdict(int)
        for forecast in future:
            by_competition[forecast["competition"]] += 1
        return jsonify({"ok": True, "now": now.isoformat(), "counts": {"forecasts": len(forecasts), "future": len(future), "past": len(past), "future_by_competition": dict(by_competition), "verified_results": len(verified) if isinstance(verified, list) else 0}, "future_forecasts": future, "verified_results": verified})
    except SupabaseError as exc:
        return jsonify({"ok": False, "error": f"supabase_http_{exc.status}", "details": exc.body}), 502
    except (RuntimeError, urllib.error.URLError) as exc:
        return jsonify({"ok": False, "error": "diagnostics_failed"}), 500

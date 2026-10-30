"""Lot 1-3 analytics: backtesting, market value, xG form, alerts, combos, Monte Carlo, live engine and scenarios."""

import json
import math
import os
import random
import urllib.parse
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations

from flask import Blueprint, jsonify, request

lot1_bp = Blueprint("lot1_analytics", __name__)

RESULTS_VIEW = "football_prospective_verified_scores_v1"
NTFY_SERVER = "https://ntfy.sh"


class SupabaseError(Exception):
    def __init__(self, status, body, path=""):
        super().__init__(f"Supabase HTTP {status}: {path}")
        self.status = status
        self.body = body
        self.path = path


def supabase_request(path, method="GET", payload=None, extra_headers=None):
    base_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base_url or not service_key:
        raise RuntimeError("missing_supabase_config")
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Prefer": "return=representation",
    }
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(
        f"{base_url}/rest/v1/{path}",
        method=method,
        headers=headers,
        data=json.dumps(payload).encode() if payload is not None else None,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise SupabaseError(exc.code, exc.read().decode(errors="replace")[:500], path) from exc


def parse_dt(value):
    return datetime.fromisoformat((value or "").replace("Z", "+00:00"))


def mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def devig_three_way(odds):
    try:
        implied = [1 / float(odds[k]) for k in ("home", "draw", "away")]
        total = sum(implied)
        if total <= 0:
            return None
        return {k: v / total for k, v in zip(("home", "draw", "away"), implied)}
    except (KeyError, TypeError, ZeroDivisionError, ValueError):
        return None


def ntfy_send(title, message, tags="soccer"):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        raise RuntimeError("missing_ntfy_topic")
    req = urllib.request.Request(
        f"{NTFY_SERVER}/{urllib.parse.quote(topic)}",
        method="POST",
        headers={"Title": title, "Tags": tags, "Content-Type": "text/plain"},
        data=message.encode(),
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return response.status


def value_opportunities():
    now = datetime.now(timezone.utc)
    forecasts = supabase_request(
        "football_experimental_forecasts_v4?select=external_id,competition,home_team,away_team,kickoff_at,home_probability,draw_probability,away_probability&order=kickoff_at.asc&limit=1000"
    )
    future = [f for f in forecasts if parse_dt(f["kickoff_at"]) > now]
    odds_rows = supabase_request(
        "live_odds?select=event_id,market,odds,provider,captured_at&order=captured_at.desc&limit=2000"
    )
    latest = {}
    for row in odds_rows:
        latest.setdefault((row["event_id"], row["market"]), row)
    opportunities = []
    for forecast in future:
        event_id = forecast.get("external_id")
        market_odds = {k: latest.get((event_id, k)) for k in ("home", "draw", "away")}
        if any(v is None for v in market_odds.values()):
            continue
        market_probs = devig_three_way({k: v["odds"] for k, v in market_odds.items()})
        if not market_probs:
            continue
        for market in ("home", "draw", "away"):
            odd = market_odds[market]
            model_probability = float(forecast[f"{market}_probability"])
            implied_probability = market_probs[market]
            edge = model_probability - implied_probability
            expected_value = model_probability * float(odd["odds"]) - 1
            if expected_value <= 0 or edge <= 0:
                continue
            opportunities.append(
                {
                    "event_id": event_id,
                    "competition": forecast["competition"],
                    "home_team": forecast["home_team"],
                    "away_team": forecast["away_team"],
                    "kickoff_at": forecast["kickoff_at"],
                    "market": market,
                    "odds": float(odd["odds"]),
                    "model_probability": model_probability,
                    "market_probability": implied_probability,
                    "market_edge": edge,
                    "expected_value": expected_value,
                    "confidence": "high" if edge >= 0.05 and expected_value >= 0.10 else "medium" if edge >= 0.03 else "low",
                    "odds_captured_at": odd["captured_at"],
                }
            )
    opportunities.sort(key=lambda x: x["expected_value"], reverse=True)
    return opportunities


def adjusted_probs(base, minute, home_score, away_score, home_reds, away_reds):
    p = {k: float(base[k]) for k in ("home", "draw", "away")}
    t = min(max(minute or 0, 0), 90) / 90
    diff = (home_score or 0) - (away_score or 0)
    if diff:
        sign = 1 if diff > 0 else -1
        lead = abs(diff)
        p["home"] *= 1 + sign * 0.8 * t * lead
        p["away"] *= 1 - sign * 0.5 * t * lead
    p["home"] *= max(0.2, 1 - 0.25 * t * (home_reds or 0))
    p["away"] *= max(0.2, 1 - 0.25 * t * (away_reds or 0))
    total = sum(p.values()) or 1
    return {k: v / total for k, v in p.items()}


@lot1_bp.get("/api/backtest")
def backtest():
    try:
        rows = supabase_request(
            f"{RESULTS_VIEW}?select=forecast_id,competition,home_team,away_team,kickoff_at,home_probability,draw_probability,away_probability,actual_outcome,log_loss,brier_score&order=kickoff_at.asc&limit=5000"
        )
        groups = defaultdict(list)
        for row in rows:
            outcome = row.get("actual_outcome")
            probs = {"home": row.get("home_probability"), "draw": row.get("draw_probability"), "away": row.get("away_probability")}
            if outcome not in probs or any(probs[k] is None for k in probs):
                continue
            kickoff = parse_dt(row["kickoff_at"])
            groups[(row["competition"], kickoff.strftime("%Y-%m"))].append(row)
        saved = []
        for (competition, month), matches in groups.items():
            log_losses, brier_scores, correct = [], [], 0
            for row in matches:
                outcome = row["actual_outcome"]
                probs = {
                    "home": max(min(float(row["home_probability"]), 0.999999), 0.000001),
                    "draw": max(min(float(row["draw_probability"]), 0.999999), 0.000001),
                    "away": max(min(float(row["away_probability"]), 0.999999), 0.000001),
                }
                log_losses.append(-math.log(probs[outcome]))
                brier_scores.append(sum((probs[k] - (1 if k == outcome else 0)) ** 2 for k in probs))
                correct += max(probs, key=probs.get) == outcome
            payload = {
                "model_version": "dixon_coles_v4",
                "competition": competition,
                "train_start": "2000-01-01",
                "train_end": month + "-01",
                "test_start": month + "-01",
                "test_end": month + "-31",
                "matches": len(matches),
                "log_loss": mean(log_losses),
                "brier_score": mean(brier_scores),
                "accuracy": correct / len(matches) if matches else None,
                "details": {"method": "walk_forward_monthly", "evaluated_at": datetime.now(timezone.utc).isoformat()},
            }
            saved.extend(
                supabase_request(
                    "model_backtests?on_conflict=model_version,competition,test_start,test_end",
                    method="POST",
                    payload=payload,
                    extra_headers={"Prefer": "resolution=merge-duplicates,return=representation"},
                )
            )
        return jsonify({"ok": True, "backtests_saved": len(saved), "groups": len(groups), "backtests": saved})
    except SupabaseError as exc:
        return jsonify({"ok": False, "error": f"supabase_http_{exc.status}:{exc.path}", "details": exc.body}), 502
    except (RuntimeError, urllib.error.URLError) as exc:
        return jsonify({"ok": False, "error": "backtest_failed", "details": str(exc)}), 500


@lot1_bp.get("/api/market-value")
def market_value():
    try:
        opportunities = value_opportunities()
        return jsonify({"ok": True, "opportunities": opportunities[:50], "count": len(opportunities)})
    except SupabaseError as exc:
        return jsonify({"ok": False, "error": f"supabase_http_{exc.status}:{exc.path}", "details": exc.body}), 502
    except (RuntimeError, urllib.error.URLError) as exc:
        return jsonify({"ok": False, "error": "market_value_failed", "details": str(exc)}), 500


@lot1_bp.get("/api/team-form")
def team_form():
    team = request.args.get("team", "").strip()
    competition = request.args.get("competition", "").strip()
    window = min(max(int(request.args.get("window", 5)), 1), 20)
    if not team or not competition:
        return jsonify({"ok": False, "error": "missing_team_or_competition"}), 400
    try:
        rows = supabase_request(
            f"team_match_stats?select=match_date,goals_for,goals_against,xg_for,xg_against&team_name=eq.{urllib.parse.quote(team)}&competition=eq.{urllib.parse.quote(competition)}&order=match_date.desc&limit={window}"
        )
        if not rows:
            return jsonify({"ok": True, "team": team, "competition": competition, "window": window, "matches": 0, "form": None})
        form = {
            "matches": len(rows),
            "xg_for_avg": mean([r.get("xg_for") for r in rows]),
            "xg_against_avg": mean([r.get("xg_against") for r in rows]),
            "goals_for_avg": mean([r.get("goals_for") for r in rows]),
            "goals_against_avg": mean([r.get("goals_against") for r in rows]),
        }
        payload = {
            "as_of_date": datetime.now(timezone.utc).date().isoformat(),
            "team_name": team,
            "competition": competition,
            "window_matches": window,
            "matches": len(rows),
            "xg_for_avg": form["xg_for_avg"],
            "xg_against_avg": form["xg_against_avg"],
            "goals_for_avg": form["goals_for_avg"],
            "goals_against_avg": form["goals_against_avg"],
        }
        supabase_request(
            "team_form_features?on_conflict=as_of_date,team_name,competition,window_matches",
            method="POST",
            payload=payload,
            extra_headers={"Prefer": "resolution=merge-duplicates,return=representation"},
        )
        return jsonify({"ok": True, "team": team, "competition": competition, "window": window, "form": form})
    except SupabaseError as exc:
        return jsonify({"ok": False, "error": f"supabase_http_{exc.status}:{exc.path}", "details": exc.body}), 502
    except (RuntimeError, urllib.error.URLError) as exc:
        return jsonify({"ok": False, "error": "team_form_failed", "details": str(exc)}), 500


@lot1_bp.post("/api/team-stats")
def ingest_team_stats():
    token = request.args.get("token", "")
    expected = os.environ.get("SUPABASE_FORECAST_TOKEN", "")
    if not expected or token != expected:
        return jsonify({"ok": False, "error": "invalid_token"}), 401
    payload = request.get_json(silent=True) or {}
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        return jsonify({"ok": False, "error": "missing_rows"}), 400
    try:
        saved = supabase_request(
            "team_match_stats?on_conflict=match_id,team_name",
            method="POST",
            payload=rows,
            extra_headers={"Prefer": "resolution=merge-duplicates,return=representation"},
        )
        return jsonify({"ok": True, "saved": len(saved)})
    except SupabaseError as exc:
        return jsonify({"ok": False, "error": f"supabase_http_{exc.status}:{exc.path}", "details": exc.body}), 502
    except (RuntimeError, urllib.error.URLError) as exc:
        return jsonify({"ok": False, "error": "team_stats_ingest_failed", "details": str(exc)}), 500


@lot1_bp.get("/api/check-alerts")
def check_alerts():
    try:
        now = datetime.now(timezone.utc)
        forecasts = supabase_request(
            "football_experimental_forecasts_v4?select=external_id,competition,home_team,away_team,kickoff_at,home_probability,draw_probability,away_probability&order=kickoff_at.asc&limit=1000"
        )
        future = [f for f in forecasts if parse_dt(f["kickoff_at"]) > now]
        odds_rows = supabase_request(
            "live_odds?select=event_id,market,odds,provider,captured_at&order=captured_at.desc&limit=2000"
        )
        latest = {}
        for row in odds_rows:
            latest.setdefault((row["event_id"], row["market"]), row)
        candidates = []
        for forecast in future:
            event_id = forecast.get("external_id")
            market_odds = {k: latest.get((event_id, k)) for k in ("home", "draw", "away")}
            if any(v is None for v in market_odds.values()):
                continue
            market_probs = devig_three_way({k: v["odds"] for k, v in market_odds.items()})
            if not market_probs:
                continue
            for market in ("home", "draw", "away"):
                odd = market_odds[market]
                model_probability = float(forecast[f"{market}_probability"])
                edge = model_probability - market_probs[market]
                expected_value = model_probability * float(odd["odds"]) - 1
                if expected_value >= 0.15 and edge >= 0.03:
                    candidates.append(
                        {
                            "event_id": event_id,
                            "competition": forecast["competition"],
                            "home_team": forecast["home_team"],
                            "away_team": forecast["away_team"],
                            "kickoff_at": forecast["kickoff_at"],
                            "market": market,
                            "outcome": market,
                            "odds": float(odd["odds"]),
                            "model_probability": model_probability,
                            "expected_value": expected_value,
                            "market_edge": edge,
                            "confidence": "high" if edge >= 0.05 else "medium",
                        }
                    )
        sent = []
        for item in candidates:
            alert_key = f"{item['event_id']}:{item['market']}:{item['odds']}"
            existing = supabase_request(f"bet_alerts?select=id&alert_key=eq.{urllib.parse.quote(alert_key)}")
            if existing:
                continue
            title = "Pari exceptionnel detecte"
            message = (
                f"{item['home_team']} - {item['away_team']}\n"
                f"Marche : {item['market']} @ {item['odds']}\n"
                f"Modele : {item['model_probability']:.1%}\n"
                f"Valeur attendue : {item['expected_value']:.1%}\n"
                f"Ecart marche : {item['market_edge']:.1%}\n"
                f"Confiance : {item['confidence']}"
            )
            ntfy_send(title, message)
            supabase_request(
                "bet_alerts",
                method="POST",
                payload={
                    "alert_key": alert_key,
                    "event_id": item["event_id"],
                    "competition": item["competition"],
                    "home_team": item["home_team"],
                    "away_team": item["away_team"],
                    "kickoff_at": item["kickoff_at"],
                    "market": item["market"],
                    "outcome": item["outcome"],
                    "odds": item["odds"],
                    "model_probability": item["model_probability"],
                    "expected_value": item["expected_value"],
                    "market_edge": item["market_edge"],
                    "confidence": item["confidence"],
                    "message": message,
                    "sent_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            sent.append(item)
        return jsonify({"ok": True, "candidates": len(candidates), "alerts_sent": len(sent), "alerts": sent})
    except SupabaseError as exc:
        return jsonify({"ok": False, "error": f"supabase_http_{exc.status}:{exc.path}", "details": exc.body}), 502
    except (RuntimeError, urllib.error.URLError) as exc:
        return jsonify({"ok": False, "error": "alert_check_failed", "details": str(exc)}), 500


@lot1_bp.get("/api/combo-recommendations")
def combo_recommendations():
    try:
        opportunities = [o for o in value_opportunities() if o["confidence"] in ("high", "medium")]
        by_event = defaultdict(list)
        for opportunity in opportunities:
            by_event[opportunity["event_id"]].append(opportunity)
        legs_pool = [max(legs, key=lambda x: x["expected_value"]) for legs in by_event.values()]
        legs_pool.sort(key=lambda x: x["expected_value"], reverse=True)
        combos = []
        for size in (2, 3):
            for legs in combinations(legs_pool[:12], size):
                events = {leg["event_id"] for leg in legs}
                if len(events) != size:
                    continue
                combined_probability = math.prod(leg["model_probability"] for leg in legs)
                combined_odds = math.prod(leg["odds"] for leg in legs)
                expected_value = combined_probability * combined_odds - 1
                if expected_value < 0.03:
                    continue
                competitions = {leg["competition"] for leg in legs}
                combos.append(
                    {
                        "legs": legs,
                        "leg_count": size,
                        "combined_odds": combined_odds,
                        "combined_probability": combined_probability,
                        "expected_value": expected_value,
                        "risk_level": "low" if len(competitions) == size else "medium",
                        "competitions": sorted(competitions),
                    }
                )
        combos.sort(key=lambda x: x["expected_value"], reverse=True)
        return jsonify({"ok": True, "combos": combos[:10], "count": len(combos)})
    except SupabaseError as exc:
        return jsonify({"ok": False, "error": f"supabase_http_{exc.status}:{exc.path}", "details": exc.body}), 502
    except (RuntimeError, urllib.error.URLError) as exc:
        return jsonify({"ok": False, "error": "combo_failed", "details": str(exc)}), 500


@lot1_bp.get("/api/monte-carlo")
def monte_carlo():
    try:
        opportunities = [o for o in value_opportunities() if o["expected_value"] > 0][:20]
        if not opportunities:
            return jsonify({"ok": True, "simulations": 0, "summary": None, "message": "Aucune opportunité disponible pour simuler une série."})
        random.seed(42)
        simulations = 1000
        results = []
        for _ in range(simulations):
            profit = 0
            for opportunity in opportunities:
                won = random.random() < opportunity["model_probability"]
                profit += opportunity["odds"] - 1 if won else -1
            results.append(profit)
        results.sort()
        profits = [r for r in results if r > 0]
        summary = {
            "bets_per_series": len(opportunities),
            "simulations": simulations,
            "average_profit": sum(results) / simulations,
            "median_profit": results[simulations // 2],
            "worst_5_percent": results[int(simulations * 0.05)],
            "best_5_percent": results[int(simulations * 0.95) - 1],
            "probability_of_loss": (simulations - len(profits)) / simulations,
            "probability_of_profit": len(profits) / simulations,
        }
        return jsonify({"ok": True, "summary": summary, "opportunities_used": len(opportunities)})
    except SupabaseError as exc:
        return jsonify({"ok": False, "error": f"supabase_http_{exc.status}:{exc.path}", "details": exc.body}), 502
    except (RuntimeError, urllib.error.URLError) as exc:
        return jsonify({"ok": False, "error": "monte_carlo_failed", "details": str(exc)}), 500


@lot1_bp.get("/api/live-matches")
def live_matches():
    try:
        events = supabase_request(
            "live_match_events?select=event_id,competition,home_team,away_team,kickoff_at,minute,home_score,away_score,home_red_cards,away_red_cards,captured_at&order=captured_at.desc&limit=500"
        )
        latest = {}
        for event in events:
            latest.setdefault(event["event_id"], event)
        forecasts = supabase_request(
            "football_experimental_forecasts_v4?select=external_id,competition,home_team,away_team,kickoff_at,home_probability,draw_probability,away_probability&order=kickoff_at.asc&limit=1000"
        )
        by_event = {f["external_id"]: f for f in forecasts}
        live = []
        for event_id, event in latest.items():
            forecast = by_event.get(event_id)
            if not forecast:
                continue
            base = {
                "home": forecast["home_probability"],
                "draw": forecast["draw_probability"],
                "away": forecast["away_probability"],
            }
            probabilities = adjusted_probs(
                base,
                event.get("minute"),
                event.get("home_score"),
                event.get("away_score"),
                event.get("home_red_cards"),
                event.get("away_red_cards"),
            )
            live.append(
                {
                    "event_id": event_id,
                    "competition": event["competition"],
                    "home_team": event["home_team"],
                    "away_team": event["away_team"],
                    "kickoff_at": event["kickoff_at"],
                    "minute": event.get("minute"),
                    "score": f"{event.get('home_score', 0)}-{event.get('away_score', 0)}",
                    "red_cards": f"{event.get('home_red_cards', 0)}-{event.get('away_red_cards', 0)}",
                    "live_probabilities": probabilities,
                    "base_probabilities": base,
                    "captured_at": event["captured_at"],
                }
            )
        return jsonify({"ok": True, "live_matches": live, "count": len(live)})
    except SupabaseError as exc:
        return jsonify({"ok": False, "error": f"supabase_http_{exc.status}:{exc.path}", "details": exc.body}), 502
    except (RuntimeError, urllib.error.URLError) as exc:
        return jsonify({"ok": False, "error": "live_matches_failed", "details": str(exc)}), 500


@lot1_bp.get("/api/match-scenarios")
def match_scenarios():
    event_id = request.args.get("event_id", "").strip()
    if not event_id:
        return jsonify({"ok": False, "error": "missing_event_id"}), 400
    try:
        forecasts = supabase_request(
            f"football_experimental_forecasts_v4?select=external_id,competition,home_team,away_team,kickoff_at,home_probability,draw_probability,away_probability&external_id=eq.{urllib.parse.quote(event_id)}&limit=1"
        )
        if not forecasts:
            return jsonify({"ok": False, "error": "forecast_not_found"}), 404
        forecast = forecasts[0]
        base = {
            "home": forecast["home_probability"],
            "draw": forecast["draw_probability"],
            "away": forecast["away_probability"],
        }
        events = supabase_request(
            f"live_match_events?select=minute,home_score,away_score,home_red_cards,away_red_cards&event_id=eq.{urllib.parse.quote(event_id)}&order=captured_at.desc&limit=1"
        )
        current = events[0] if events else {"minute": 0, "home_score": 0, "away_score": 0, "home_red_cards": 0, "away_red_cards": 0}
        scenarios = {
            "current": adjusted_probs(base, current.get("minute"), current.get("home_score"), current.get("away_score"), current.get("home_red_cards"), current.get("away_red_cards")),
            "home_leads_1_0_at_30": adjusted_probs(base, 30, 1, 0, current.get("home_red_cards"), current.get("away_red_cards")),
            "scoreless_at_60": adjusted_probs(base, 60, 0, 0, current.get("home_red_cards"), current.get("away_red_cards")),
            "home_red_card_at_60": adjusted_probs(base, 60, current.get("home_score"), current.get("away_score"), 1, current.get("away_red_cards")),
            "away_red_card_at_60": adjusted_probs(base, 60, current.get("home_score"), current.get("away_score"), current.get("home_red_cards"), 1),
        }
        return jsonify({"ok": True, "event_id": event_id, "match": f"{forecast['home_team']} - {forecast['away_team']}", "scenarios": scenarios})
    except SupabaseError as exc:
        return jsonify({"ok": False, "error": f"supabase_http_{exc.status}:{exc.path}", "details": exc.body}), 502
    except (RuntimeError, urllib.error.URLError) as exc:
        return jsonify({"ok": False, "error": "scenarios_failed", "details": str(exc)}), 500

"""Lot 1 analytics: walk-forward backtesting, market value, xG form and smart alerts."""

import json
import math
import os
import urllib.parse
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, timezone

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


@lot1_bp.get("/api/backtest")
def backtest():
    try:
        rows = supabase_request(
            f"{RESULTS_VIEW}?select=forecast_id,competition,home_team,away_team,kickoff_at,home_probability,draw_probability,away_probability,actual_outcome,log_loss,brier_score&order=kickoff_at.asc&limit=5000"
        )
        groups = defaultdict(list)
        for row in rows:
            outcome = row.get("actual_outcome")
            probs = {
                "home": row.get("home_probability"),
                "draw": row.get("draw_probability"),
                "away": row.get("away_probability"),
            }
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
        now = datetime.now(timezone.utc)
        forecasts = supabase_request(
            "football_experimental_forecasts_v4?select=external_id,competition,home_team,away_team,kickoff_at,home_probability,draw_probability,away_probability,protected_forecast&order=kickoff_at.asc&limit=1000"
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

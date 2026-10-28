"""Application performance API: hit rate, ROI, CLV and calibration tracking."""

import json
import math
import os
import urllib.request
import urllib.error
from collections import defaultdict
from flask import Flask, jsonify

app = Flask(__name__)

RESULTS_VIEW = "football_model_prospective_results_v4"


class SupabaseError(Exception):
    def __init__(self, status, body):
        super().__init__(f"Supabase HTTP {status}")
        self.status = status
        self.body = body


def supabase_request(path):
    base_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base_url or not service_key:
        raise RuntimeError("missing_supabase_config")
    req = urllib.request.Request(
        f"{base_url}/rest/v1/{path}",
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise SupabaseError(exc.code, exc.read().decode(errors="replace")[:500]) from exc


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def aggregate(rows):
    settled = [r for r in rows if r.get("result") in ("won", "lost", "void")]
    won = sum(r.get("result") == "won" for r in settled)
    profit = sum(float(r["profit"]) for r in settled if r.get("profit") is not None)
    staked = sum(float(r.get("stake_amount") or 0) for r in settled)
    clv = [float(r["closing_line_value"]) for r in settled if r.get("closing_line_value") is not None]
    return {
        "bets": len(rows),
        "settled": len(settled),
        "won": won,
        "hit_rate": ratio(won, len(settled)),
        "staked": staked,
        "profit": profit,
        "roi": ratio(profit, staked),
        "average_clv": ratio(sum(clv), len(clv)),
    }


def prediction_metrics(rows):
    log_losses = []
    brier_scores = []
    for row in rows:
        outcome = row.get("actual_outcome")
        probs = {
            "home": row.get("home_probability"),
            "draw": row.get("draw_probability"),
            "away": row.get("away_probability"),
        }
        if outcome not in probs or any(probs[k] is None for k in probs):
            continue
        probs = {k: max(min(float(v), 0.999999), 0.000001) for k, v in probs.items()}
        log_losses.append(-math.log(probs[outcome]))
        brier_scores.append(sum((probs[k] - (1 if k == outcome else 0)) ** 2 for k in probs))
    return {
        "matches": len(log_losses),
        "average_log_loss": ratio(sum(log_losses), len(log_losses)),
        "average_brier_score": ratio(sum(brier_scores), len(brier_scores)),
    }


@app.get("/api/performance")
def performance():
    try:
        bets = supabase_request("bet_history?select=*&order=created_at.desc&limit=1000")
        overall = aggregate(bets)

        by_profile = defaultdict(list)
        by_strategy = defaultdict(list)
        by_month = defaultdict(list)
        for bet in bets:
            by_profile[bet.get("profile") or "unknown"].append(bet)
            by_strategy[bet.get("strategy") or bet.get("profile") or "unknown"].append(bet)
            month = (bet.get("created_at") or "")[:7]
            if month:
                by_month[month].append(bet)

        try:
            results = supabase_request(
                f"{RESULTS_VIEW}?select=home_probability,draw_probability,away_probability,actual_outcome"
                "&actual_outcome=not.is.null"
                "&order=kickoff_at.desc&limit=1000"
            )
            quality = prediction_metrics(results)
        except SupabaseError:
            quality = {"matches": 0, "average_log_loss": None, "average_brier_score": None}

        return jsonify({
            "ok": True,
            "overall": overall,
            "by_profile": {k: aggregate(v) for k, v in sorted(by_profile.items())},
            "by_strategy": {k: aggregate(v) for k, v in sorted(by_strategy.items())},
            "by_month": {k: aggregate(v) for k, v in sorted(by_month.items())},
            "prediction_quality": quality,
        })
    except SupabaseError as exc:
        return jsonify({"ok": False, "error": f"supabase_http_{exc.status}", "details": exc.body}), 502
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

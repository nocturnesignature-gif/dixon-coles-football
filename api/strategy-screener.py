"""Read-only Strategy Copilot Edge Screener.

Reads only football_clv_reconciliation_v1 and returns prospective candidates
whose opening odds were captured before the forecast input cutoff.
"""
import json
import math
import os
import urllib.parse
import urllib.request
from flask import Flask, jsonify, request

app = Flask(__name__)
DISCLAIMER = "Estimations expérimentales — calibration prospective non validée. Aucune recommandation de pari."


def supabase_get(path):
    base = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base or not key:
        raise RuntimeError("missing_supabase_config")
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }
    req = urllib.request.Request(f"{base}/rest/v1/{path}", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def decimal_probability(odds):
    value = float(odds)
    if not math.isfinite(value) or value <= 1:
        return None
    return 1.0 / value


@app.get("/api/strategy-screener")
def strategy_screener():
    try:
        min_edge = max(0.0, float(request.args.get("min_edge", "0.045")))
        limit = min(200, max(1, int(request.args.get("limit", "50"))))
        competition = request.args.get("competition", "").strip()

        params = {
            "select": "forecast_id,event_id,competition,home_team,away_team,kickoff_at,market,home_probability,draw_probability,away_probability,opening_odds,opening_provider,opening_captured_at,input_cutoff_at,has_opening_odds",
            "has_opening_odds": "eq.true",
            "market": "eq.h2h",
            "order": "kickoff_at.asc",
            "limit": str(limit * 3),
        }
        if competition:
            params["competition"] = f"eq.{competition}"

        rows = supabase_get(f"football_clv_reconciliation_v1?{urllib.parse.urlencode(params)}")
        by_forecast = {}
        for row in rows:
            opening = row.get("opening_captured_at")
            cutoff = row.get("input_cutoff_at")
            if not opening or not cutoff or opening > cutoff:
                continue
            by_forecast.setdefault(row["forecast_id"], []).append(row)

        candidates = []
        for forecast_id, outcomes in by_forecast.items():
            prices = {}
            model = {}
            for outcome in outcomes:
                market = outcome.get("market")
                price = decimal_probability(outcome.get("opening_odds"))
                probability = outcome.get(f"{market}_probability")
                if price is None or probability is None:
                    continue
                prices[market] = price
                model[market] = float(probability)

            if set(prices) != {"home", "draw", "away"}:
                continue

            raw_total = sum(prices.values())
            if raw_total <= 0:
                continue

            for market in ("home", "draw", "away"):
                implied_net = prices[market] / raw_total
                edge = model[market] - implied_net
                if edge < min_edge:
                    continue
                base = outcomes[0]
                candidates.append({
                    "forecast_id": forecast_id,
                    "event_id": base.get("event_id"),
                    "competition": base.get("competition"),
                    "home_team": base.get("home_team"),
                    "away_team": base.get("away_team"),
                    "kickoff_at": base.get("kickoff_at"),
                    "market": "h2h",
                    "selection": market,
                    "model_probability": model[market],
                    "implied_probability_raw": prices[market],
                    "implied_probability_net": implied_net,
                    "edge": edge,
                    "opening_odds": base.get("opening_odds"),
                    "opening_provider": base.get("opening_provider"),
                    "opening_captured_at": base.get("opening_captured_at"),
                    "input_cutoff_at": base.get("input_cutoff_at"),
                })

        candidates.sort(key=lambda item: item["edge"], reverse=True)
        candidates = candidates[:limit]
        return jsonify({
            "ok": True,
            "disclaimer": DISCLAIMER,
            "min_edge": min_edge,
            "candidate_count": len(candidates),
            "candidates": candidates,
        })
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc), "disclaimer": DISCLAIMER}), 500
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
        return jsonify({"ok": False, "error": "strategy_screener_failed", "details": str(exc), "disclaimer": DISCLAIMER}), 500


if __name__ == "__main__":
    app.run()

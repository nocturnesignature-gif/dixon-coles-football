"""Paper Bot analytics: championship breakdown and Monte Carlo robustness.

Read-only analytics over virtual paper-trading positions. Monte Carlo is
enabled only after a minimum number of settled positions.
"""
import json
import math
import os
import random
import urllib.parse
import urllib.request
from flask import Flask, jsonify, request

app = Flask(__name__)
DISCLAIMER = "Estimations expérimentales — calibration prospective non validée. Aucune recommandation de pari."
MIN_SETTLED_FOR_MONTE_CARLO = 30


def sb(path):
    base = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base or not key:
        raise RuntimeError("missing_supabase_config")
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"}
    req = urllib.request.Request(f"{base}/rest/v1/{path}", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def championship_analytics():
    positions = sb("virtual_portfolio_positions?order=placed_at.desc&limit=500")
    legs = sb("virtual_portfolio_legs?limit=1000")
    candidates = sb("strategy_candidates?limit=1000")
    candidate_by_id = {c["id"]: c for c in candidates}
    leg_by_position = {}
    for leg in legs:
        leg_by_position.setdefault(leg["position_id"], []).append(leg)

    rows = []
    for position in positions:
        position_legs = leg_by_position.get(position["id"], [])
        for leg in position_legs:
            candidate = candidate_by_id.get(leg["candidate_id"], {})
            rows.append({
                "portfolio_id": position["portfolio_id"],
                "strategy_type": position["strategy_type"],
                "competition": candidate.get("competition") or "Inconnue",
                "status": position["status"],
                "profit_loss": float(position.get("profit_loss") or 0),
                "stake_amount": float(position.get("stake_amount") or 0),
                "edge": float(leg.get("edge") or 0),
            })

    grouped = {}
    for row in rows:
        key = (row["strategy_type"], row["competition"])
        grouped.setdefault(key, []).append(row)

    analytics = []
    for (strategy_type, competition), items in sorted(grouped.items()):
        settled = [x for x in items if x["status"] in ("won", "lost", "push", "void")]
        wins = sum(1 for x in settled if x["status"] == "won")
        pnl = sum(x["profit_loss"] for x in settled)
        stakes = sum(x["stake_amount"] for x in settled)
        analytics.append({
            "strategy_type": strategy_type,
            "competition": competition,
            "positions": len(items),
            "settled": len(settled),
            "wins": wins,
            "win_rate": wins / len(settled) if settled else None,
            "realized_pnl": round(pnl, 2),
            "roi": round(pnl / stakes, 4) if stakes else None,
            "average_edge": round(sum(x["edge"] for x in items) / len(items), 4) if items else None,
        })
    return analytics


def monte_carlo():
    positions = sb("virtual_portfolio_positions?status=in.(won,lost,push,void)&limit=1000")
    settled = [p for p in positions if p.get("profit_loss") is not None and float(p["stake_amount"]) > 0]
    if len(settled) < MIN_SETTLED_FOR_MONTE_CARLO:
        return {"enabled": False, "reason": "insufficient_settled_positions", "settled_positions": len(settled), "minimum_required": MIN_SETTLED_FOR_MONTE_CARLO}

    returns = [float(p["profit_loss"]) / float(p["stake_amount"]) for p in settled]
    simulations, horizon = 1000, 100
    final_bankrolls, max_drawdowns = [], []
    for _ in range(simulations):
        bankroll, peak, drawdown = 10000.0, 10000.0, 0.0
        for _ in range(horizon):
            bankroll *= 1.0 + random.choice(returns)
            peak = max(peak, bankroll)
            drawdown = max(drawdown, (peak - bankroll) / peak)
        final_bankrolls.append(bankroll)
        max_drawdowns.append(drawdown)
    final_bankrolls.sort()
    max_drawdowns.sort()
    return {
        "enabled": True,
        "settled_positions": len(settled),
        "simulations": simulations,
        "horizon": horizon,
        "bankroll_p5": round(final_bankrolls[int(0.05 * (simulations - 1))], 2),
        "bankroll_p50": round(final_bankrolls[int(0.50 * (simulations - 1))], 2),
        "bankroll_p95": round(final_bankrolls[int(0.95 * (simulations - 1))], 2),
        "max_drawdown_p50": round(max_drawdowns[int(0.50 * (simulations - 1))], 4),
        "max_drawdown_p95": round(max_drawdowns[int(0.95 * (simulations - 1))], 4),
    }


@app.get("/api/paper-bot/analytics")
def analytics():
    try:
        return jsonify({
            "ok": True,
            "disclaimer": DISCLAIMER,
            "championship_analytics": championship_analytics(),
            "monte_carlo": monte_carlo(),
        })
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc), "disclaimer": DISCLAIMER}), 500
    except Exception as exc:
        return jsonify({"ok": False, "error": "paper_bot_analytics_failed", "details": str(exc), "disclaimer": DISCLAIMER}), 500


from flask import request

if __name__ == "__main__":
    app.run()

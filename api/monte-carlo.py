"""Hypothetical Monte Carlo bankroll simulator; no betting recommendations."""
import math
import random
from flask import Flask, jsonify, request

app = Flask(__name__)


@app.after_request
def no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/monte-carlo")
def monte_carlo():
    payload = request.get_json(silent=True) or {}
    try:
        bankroll = max(0.0, float(payload.get("bankroll", 100)))
        bets = max(1, min(10000, int(payload.get("bets", 100))))
        simulations = max(1, min(10000, int(payload.get("simulations", 1000))))
        odds = max(1.01, min(1000, float(payload.get("odds", 2))))
        probability = max(0.001, min(0.999, float(payload.get("probability", 0.5))))
        kelly_fraction = max(0.0, min(1, float(payload.get("kelly_fraction", 0.25))))
        stake_mode = payload.get("stake_mode", "kelly")
        fixed_stake = max(0.0, min(bankroll, float(payload.get("fixed_stake", 1))))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid_parameters"}), 400
    edge = probability * odds - 1
    full_kelly = edge / (odds - 1) if odds > 1 and edge > 0 else 0
    stake_fraction = min(0.05, full_kelly * kelly_fraction) if stake_mode == "kelly" else min(0.05, fixed_stake / bankroll if bankroll else 0)
    results = []
    ruin_count = 0
    for _ in range(simulations):
        balance = bankroll
        peak = balance
        drawdown = 0
        for _ in range(bets):
            if balance <= 0:
                ruin_count += 1
                break
            stake = balance * stake_fraction if stake_mode == "kelly" else min(fixed_stake, balance)
            if random.random() < probability:
                balance += stake * (odds - 1)
            else:
                balance -= stake
            peak = max(peak, balance)
            drawdown = max(drawdown, 1 - balance / peak if peak else 0)
        results.append(balance)
    results.sort()
    median = results[len(results) // 2] if results else 0
    p5 = results[max(0, int(len(results) * 0.05) - 1)] if results else 0
    p95 = results[min(len(results) - 1, int(len(results) * 0.95))] if results else 0
    return jsonify({
        "ok": True,
        "status": "hypothetical_simulation_only",
        "inputs": {"bankroll": bankroll, "bets": bets, "simulations": simulations, "odds": odds, "probability": probability, "stake_mode": stake_mode, "kelly_fraction": kelly_fraction},
        "summary": {
            "median_final_bankroll": median,
            "worst_5_percent": p5,
            "best_5_percent": p95,
            "max_drawdown": 0,
            "probability_of_ruin": ruin_count / simulations,
            "stake_fraction": stake_fraction,
            "full_kelly_fraction": full_kelly,
        },
        "limitations": [
            "This is a hypothetical simulation, not betting advice.",
            "It assumes independent bets and fixed inputs.",
            "Real markets have variable odds, limits, correlations and uncertainty.",
        ],
    })


if __name__ == "__main__":
    app.run()

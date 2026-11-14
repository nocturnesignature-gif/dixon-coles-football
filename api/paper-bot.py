"""Paper Bot: virtual, non-monetary strategy simulator.

Reads prospective candidates from football_clv_reconciliation_v1 and creates at
most one open virtual position per portfolio per manual simulation run.
"""
import json
import math
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from flask import Flask, jsonify, request

app = Flask(__name__)
DISCLAIMER = "Estimations expérimentales — calibration prospective non validée. Aucune recommandation de pari."
FILTER_NAME = "paper-bot-edge-4-5"


def sb(method, path, payload=None, prefer=None):
    base = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base or not key:
        raise RuntimeError("missing_supabase_config")
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept": "application/json"}
    if prefer:
        headers["Prefer"] = prefer
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{base}/rest/v1/{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as response:
        body = response.read()
        return json.loads(body) if body else []


def get_candidates(min_edge=0.045, limit=100):
    params = urllib.parse.urlencode({
        "select": "forecast_id,event_id,competition,home_team,away_team,kickoff_at,market,home_probability,draw_probability,away_probability,opening_odds,opening_provider,opening_captured_at,input_cutoff_at,has_opening_odds",
        "has_opening_odds": "eq.true",
        "order": "kickoff_at.asc",
        "limit": "300",
    })
    rows = sb("GET", f"football_clv_reconciliation_v1?{params}")
    grouped = {}
    for row in rows:
        if not row.get("opening_captured_at") or not row.get("input_cutoff_at"):
            continue
        if row["opening_captured_at"] > row["input_cutoff_at"]:
            continue
        grouped.setdefault(row["forecast_id"], []).append(row)
    candidates = []
    for forecast_id, outcomes in grouped.items():
        prices, model, rows_by_market = {}, {}, {}
        for row in outcomes:
            market = row.get("market")
            odds = row.get("opening_odds")
            probability = row.get(f"{market}_probability") if market in ("home", "draw", "away") else None
            if not odds or odds <= 1 or probability is None:
                continue
            prices[market] = 1.0 / float(odds)
            model[market] = float(probability)
            rows_by_market[market] = row
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
            base = rows_by_market[market]
            candidates.append({"forecast_id": forecast_id, "event_id": base.get("event_id"), "competition": base.get("competition"), "home_team": base.get("home_team"), "away_team": base.get("away_team"), "kickoff_at": base.get("kickoff_at"), "market": market, "selection": market, "model_probability": model[market], "implied_probability_net": implied_net, "edge": edge, "opening_odds": float(base["opening_odds"]), "opening_provider": base.get("opening_provider"), "opening_captured_at": base.get("opening_captured_at"), "input_cutoff_at": base.get("input_cutoff_at")})
    candidates.sort(key=lambda x: x["edge"], reverse=True)
    return candidates[:limit]


def ensure_filter():
    rows = sb("GET", f"strategy_filters?name=eq.{urllib.parse.quote(FILTER_NAME)}")
    if rows:
        return rows[0]["id"]
    return sb("POST", "strategy_filters", {"name": FILTER_NAME, "competitions": [], "markets": ["home", "draw", "away"], "min_edge": 0.045, "bankroll": 0, "kelly_fraction": 0.25, "max_stake_fraction": 0.01, "is_active": True}, "return=representation")[0]["id"]


def ensure_portfolio(name, strategy_type):
    rows = sb("GET", f"virtual_portfolios?name=eq.{urllib.parse.quote(name)}")
    if rows:
        return rows[0]
    return sb("POST", "virtual_portfolios", {"name": name, "strategy_type": strategy_type, "initial_bankroll": 10000, "current_bankroll": 10000, "kelly_fraction": 0.25, "max_stake_fraction": 0.01, "is_active": True}, "return=representation")[0]


def upsert_candidate(filter_id, candidate):
    payload = {"filter_id": filter_id, "forecast_id": candidate["forecast_id"], "event_id": candidate.get("event_id"), "competition": candidate.get("competition"), "home_team": candidate.get("home_team"), "away_team": candidate.get("away_team"), "market": candidate["market"], "selection": candidate["selection"], "model_probability": candidate["model_probability"], "implied_probability": candidate["implied_probability_net"], "edge": candidate["edge"], "opening_odds": candidate["opening_odds"], "opening_provider": candidate.get("opening_provider"), "opening_captured_at": candidate.get("opening_captured_at"), "input_cutoff_at": candidate.get("input_cutoff_at"), "kickoff_at": candidate.get("kickoff_at"), "status": "eligible"}
    return sb("POST", "strategy_candidates?on_conflict=filter_id,forecast_id,market,selection", [payload], "return=representation,resolution=merge-duplicates")[0]["id"]


def kelly_stake(bankroll, edge, odds, kelly_fraction, max_fraction):
    if odds <= 1 or edge <= 0:
        return 0.0
    return round(min(max(0.0, bankroll * kelly_fraction * (edge / (odds - 1.0))), bankroll * max_fraction), 2)


def create_position(portfolio, strategy_type, stake, legs):
    if stake <= 0 or not legs:
        return None
    combined_odds = math.prod(leg["odds_taken"] for leg in legs)
    position = sb("POST", "virtual_portfolio_positions", {"portfolio_id": portfolio["id"], "strategy_type": strategy_type, "stake_amount": stake, "combined_odds": combined_odds, "status": "open", "placed_at": datetime.now(timezone.utc).isoformat()}, "return=representation")[0]
    for leg in legs:
        sb("POST", "virtual_portfolio_legs", {"position_id": position["id"], "candidate_id": leg["candidate_id"], "selection": leg["selection"], "model_probability": leg["model_probability"], "implied_probability_net": leg["implied_probability_net"], "edge": leg["edge"], "opening_odds": leg["opening_odds"], "odds_taken": leg["odds_taken"], "status": "pending"})
    new_bankroll = round(float(portfolio["current_bankroll"]) - stake, 2)
    new_exposure = round(float(portfolio["open_exposure"]) + stake, 2)
    sb("PATCH", f"virtual_portfolios?id=eq.{portfolio['id']}", {"current_bankroll": new_bankroll, "open_exposure": new_exposure, "updated_at": datetime.now(timezone.utc).isoformat()})
    return {"position_id": position["id"], "stake": stake, "combined_odds": combined_odds, "legs": len(legs)}


@app.get("/api/paper-bot")
def status():
    try:
        portfolios = sb("GET", "virtual_portfolios?order=name.asc")
        positions = sb("GET", "virtual_portfolio_positions?order=placed_at.desc&limit=50")
        legs = sb("GET", "virtual_portfolio_legs?order=created_at.desc&limit=500")
        candidate_ids = list({leg["candidate_id"] for leg in legs})
        candidates = []
        if candidate_ids:
            ids = ",".join(candidate_ids)
            candidates = sb("GET", f"strategy_candidates?id=in.({ids})")
        candidates_by_id = {candidate["id"]: candidate for candidate in candidates}
        enriched_legs = []
        for leg in legs:
            candidate = candidates_by_id.get(leg["candidate_id"], {})
            enriched_legs.append({**leg, "home_team": candidate.get("home_team"), "away_team": candidate.get("away_team"), "competition": candidate.get("competition"), "kickoff_at": candidate.get("kickoff_at")})
        return jsonify({"ok": True, "disclaimer": DISCLAIMER, "portfolios": portfolios, "positions": positions, "legs": enriched_legs})
    except Exception as exc:
        return jsonify({"ok": False, "error": "paper_bot_status_failed", "details": str(exc), "disclaimer": DISCLAIMER}), 500


@app.post("/api/paper-bot/simulate")
def simulate():
    try:
        filter_id = ensure_filter()
        alpha = ensure_portfolio("Bot Singles - Alpha", "singles")
        beta = ensure_portfolio("Bot Combinés - Beta", "combo_2")
        candidates = get_candidates()
        results = {"alpha": None, "beta": None}
        open_positions = sb("GET", "virtual_portfolio_positions?status=eq.open")
        open_portfolios = {p["portfolio_id"] for p in open_positions}
        if alpha["id"] not in open_portfolios and candidates:
            best = candidates[0]
            candidate_id = upsert_candidate(filter_id, best)
            stake = kelly_stake(float(alpha["current_bankroll"]), best["edge"], best["opening_odds"], float(alpha["kelly_fraction"]), float(alpha["max_stake_fraction"]))
            leg = {"candidate_id": candidate_id, **best, "odds_taken": best["opening_odds"]}
            results["alpha"] = create_position(alpha, "singles", stake, [leg])
        if beta["id"] not in open_portfolios and len(candidates) >= 2:
            chosen, seen = [], set()
            for candidate in candidates:
                if candidate["forecast_id"] in seen:
                    continue
                chosen.append(candidate)
                seen.add(candidate["forecast_id"])
                if len(chosen) == 2:
                    break
            if len(chosen) == 2:
                legs = []
                for candidate in chosen:
                    candidate_id = upsert_candidate(filter_id, candidate)
                    legs.append({"candidate_id": candidate_id, **candidate, "odds_taken": candidate["opening_odds"]})
                combined_probability = math.prod(leg["model_probability"] for leg in legs)
                combined_odds = math.prod(leg["odds_taken"] for leg in legs)
                combined_edge = combined_probability - (1.0 / combined_odds)
                stake = kelly_stake(float(beta["current_bankroll"]), combined_edge, combined_odds, float(beta["kelly_fraction"]), float(beta["max_stake_fraction"]))
                results["beta"] = create_position(beta, "combo_2", stake, legs)
        return jsonify({"ok": True, "disclaimer": DISCLAIMER, "results": results, "candidate_count": len(candidates)})
    except Exception as exc:
        return jsonify({"ok": False, "error": "paper_bot_simulation_failed", "details": str(exc), "disclaimer": DISCLAIMER}), 500


if __name__ == "__main__":
    app.run()

"""Autonomous Paper Bot loop: virtual positions only.

Settles mature positions from observed results, then creates at most one new
virtual position per portfolio when a prospective candidate passes all filters.
"""
import json
import math
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from flask import Flask, jsonify

app = Flask(__name__)
DISCLAIMER = "Estimations expérimentales — calibration prospective non validée. Aucune recommandation de pari."
FILTER_NAME = "paper-bot-edge-4-5"
TOP_LEAGUES = {"Premier League", "Serie A", "Bundesliga", "Ligue 1", "Primera Division"}


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


def log(run_id, action, portfolio_id=None, reason=None, details=None):
    sb("POST", "paper_bot_activity_log", {"run_id": run_id, "action": action, "portfolio_id": portfolio_id, "reason": reason, "details": details or {}})


def get_candidates(min_edge=0.045):
    params = urllib.parse.urlencode({
        "select": "forecast_id,event_id,competition,home_team,away_team,kickoff_at,market,home_probability,draw_probability,away_probability,opening_odds,opening_provider,opening_captured_at,input_cutoff_at,has_opening_odds",
        "has_opening_odds": "eq.true",
        "order": "kickoff_at.asc",
        "limit": "300",
    })
    rows = sb("GET", f"football_clv_reconciliation_v1?{params}")
    grouped = {}
    for row in rows:
        if row.get("opening_captured_at") and row.get("input_cutoff_at") and row["opening_captured_at"] <= row["input_cutoff_at"]:
            grouped.setdefault(row["forecast_id"], []).append(row)
    candidates = []
    for forecast_id, outcomes in grouped.items():
        prices, model, base_rows = {}, {}, {}
        for row in outcomes:
            market = row.get("market")
            odds = row.get("opening_odds")
            probability = row.get(f"{market}_probability") if market in ("home", "draw", "away") else None
            if not odds or odds <= 1 or probability is None:
                continue
            prices[market] = 1.0 / float(odds)
            model[market] = float(probability)
            base_rows[market] = row
        if set(prices) != {"home", "draw", "away"}:
            continue
        raw_total = sum(prices.values())
        for market in ("home", "draw", "away"):
            implied_net = prices[market] / raw_total
            edge = model[market] - implied_net
            if edge < min_edge:
                continue
            base = base_rows[market]
            candidates.append({"forecast_id": forecast_id, "event_id": base.get("event_id"), "competition": base.get("competition"), "home_team": base.get("home_team"), "away_team": base.get("away_team"), "kickoff_at": base.get("kickoff_at"), "market": market, "selection": market, "model_probability": model[market], "implied_probability_net": implied_net, "edge": edge, "opening_odds": float(base["opening_odds"]), "opening_provider": base.get("opening_provider"), "opening_captured_at": base.get("opening_captured_at"), "input_cutoff_at": base.get("input_cutoff_at")})
    candidates.sort(key=lambda x: x["edge"], reverse=True)
    return candidates


def ensure_filter():
    rows = sb("GET", f"strategy_filters?name=eq.{urllib.parse.quote(FILTER_NAME)}")
    if rows:
        return rows[0]["id"]
    return sb("POST", "strategy_filters", {"name": FILTER_NAME, "competitions": [], "markets": ["home", "draw", "away"], "min_edge": 0.045, "bankroll": 0, "kelly_fraction": 0.25, "max_stake_fraction": 0.01, "is_active": True}, "return=representation")[0]["id"]


def upsert_candidate(filter_id, candidate):
    payload = {"filter_id": filter_id, "forecast_id": candidate["forecast_id"], "event_id": candidate.get("event_id"), "competition": candidate.get("competition"), "home_team": candidate.get("home_team"), "away_team": candidate.get("away_team"), "market": candidate["market"], "selection": candidate["selection"], "model_probability": candidate["model_probability"], "implied_probability": candidate["implied_probability_net"], "edge": candidate["edge"], "opening_odds": candidate["opening_odds"], "opening_provider": candidate.get("opening_provider"), "opening_captured_at": candidate.get("opening_captured_at"), "input_cutoff_at": candidate.get("input_cutoff_at"), "kickoff_at": candidate.get("kickoff_at"), "status": "eligible"}
    return sb("POST", "strategy_candidates?on_conflict=filter_id,forecast_id,market,selection", [payload], "return=representation,resolution=merge-duplicates")[0]["id"]


def kelly_stake(bankroll, edge, odds, kelly_fraction, max_fraction):
    if odds <= 1 or edge <= 0:
        return 0.0
    return round(min(max(0.0, bankroll * kelly_fraction * (edge / (odds - 1.0))), bankroll * max_fraction), 2)


def settle_positions(run_id):
    positions = sb("GET", "virtual_portfolio_positions?status=eq.open")
    settled = 0
    for position in positions:
        legs = sb("GET", f"virtual_portfolio_legs?position_id=eq.{position['id']}")
        if not legs:
            continue
        candidate = sb("GET", f"strategy_candidates?id=eq.{legs[0]['candidate_id']}")[0]
        results = sb("GET", f"football_clv_reconciliation_v1?forecast_id=eq.{candidate['forecast_id']}&has_result=eq.true&limit=1")
        if not results:
            continue
        outcome = results[0].get("actual_outcome")
        if not outcome:
            continue
        statuses = []
        for leg in legs:
            leg_status = "won" if leg["selection"] == outcome else "lost"
            statuses.append(leg_status)
            sb("PATCH", f"virtual_portfolio_legs?id=eq.{leg['id']}", {"status": leg_status, "result": outcome, "updated_at": datetime.now(timezone.utc).isoformat()})
        if "lost" in statuses:
            position_status, pnl = "lost", -float(position["stake_amount"])
        elif all(status == "won" for status in statuses):
            position_status, pnl = "won", float(position["stake_amount"]) * (float(position["combined_odds"]) - 1.0)
        else:
            position_status, pnl = "void", 0.0
        sb("PATCH", f"virtual_portfolio_positions?id=eq.{position['id']}", {"status": position_status, "settled_at": datetime.now(timezone.utc).isoformat(), "profit_loss": pnl, "updated_at": datetime.now(timezone.utc).isoformat()})
        portfolio = sb("GET", f"virtual_portfolios?id=eq.{position['portfolio_id']}")[0]
        refund = float(position["stake_amount"]) if position_status in ("won", "void") else 0.0
        new_bankroll = round(float(portfolio["current_bankroll"]) + refund + max(0.0, pnl), 2)
        new_exposure = round(max(0.0, float(portfolio["open_exposure"]) - float(position["stake_amount"])), 2)
        new_pnl = round(float(portfolio["realized_pnl"]) + pnl, 2)
        sb("PATCH", f"virtual_portfolios?id=eq.{portfolio['id']}", {"current_bankroll": new_bankroll, "open_exposure": new_exposure, "realized_pnl": new_pnl, "updated_at": datetime.now(timezone.utc).isoformat()})
        log(run_id, "settlement", portfolio["id"], f"Position {position_status}", {"position_id": position["id"], "pnl": pnl, "outcome": outcome})
        settled += 1
    return settled


def create_positions(run_id, filter_id, portfolios, candidates):
    created = 0
    open_positions = sb("GET", "virtual_portfolio_positions?status=eq.open")
    open_portfolios = {p["portfolio_id"] for p in open_positions}
    for portfolio in portfolios:
        if portfolio["id"] in open_portfolios or not candidates:
            log(run_id, "rejection", portfolio["id"], "no_new_position", {"reason": "open_position_or_no_candidates"})
            continue
        eligible = [c for c in candidates if c["competition"] in TOP_LEAGUES]
        if portfolio["strategy_type"] == "singles" and eligible:
            best = eligible[0]
            candidate_id = upsert_candidate(filter_id, best)
            stake = kelly_stake(float(portfolio["current_bankroll"]), best["edge"], best["opening_odds"], float(portfolio["kelly_fraction"]), 0.01)
            if stake > 0:
                position = sb("POST", "virtual_portfolio_positions", {"portfolio_id": portfolio["id"], "strategy_type": "singles", "stake_amount": stake, "combined_odds": best["opening_odds"], "status": "open", "placed_at": datetime.now(timezone.utc).isoformat()}, "return=representation")[0]
                sb("POST", "virtual_portfolio_legs", {"position_id": position["id"], "candidate_id": candidate_id, "selection": best["selection"], "model_probability": best["model_probability"], "implied_probability_net": best["implied_probability_net"], "edge": best["edge"], "opening_odds": best["opening_odds"], "odds_taken": best["opening_odds"], "status": "pending"})
                sb("PATCH", f"virtual_portfolios?id=eq.{portfolio['id']}", {"current_bankroll": round(float(portfolio["current_bankroll"]) - stake, 2), "open_exposure": round(float(portfolio["open_exposure"]) + stake, 2), "updated_at": datetime.now(timezone.utc).isoformat()})
                log(run_id, "decision", portfolio["id"], "single_position_created", {"candidate": best, "stake": stake})
                created += 1
        elif portfolio["strategy_type"] == "combo_2" and len(eligible) >= 2:
            chosen, seen = [], set()
            for candidate in eligible:
                if candidate["forecast_id"] in seen:
                    continue
                chosen.append(candidate)
                seen.add(candidate["forecast_id"])
                if len(chosen) == 2:
                    break
            if len(chosen) == 2:
                combined_odds = math.prod(c["opening_odds"] for c in chosen)
                combined_probability = math.prod(c["model_probability"] for c in chosen)
                combined_edge = combined_probability - (1.0 / combined_odds)
                stake = kelly_stake(float(portfolio["current_bankroll"]), combined_edge, combined_odds, float(portfolio["kelly_fraction"]), 0.005)
                if stake > 0:
                    position = sb("POST", "virtual_portfolio_positions", {"portfolio_id": portfolio["id"], "strategy_type": "combo_2", "stake_amount": stake, "combined_odds": combined_odds, "status": "open", "placed_at": datetime.now(timezone.utc).isoformat()}, "return=representation")[0]
                    for candidate in chosen:
                        candidate_id = upsert_candidate(filter_id, candidate)
                        sb("POST", "virtual_portfolio_legs", {"position_id": position["id"], "candidate_id": candidate_id, "selection": candidate["selection"], "model_probability": candidate["model_probability"], "implied_probability_net": candidate["implied_probability_net"], "edge": candidate["edge"], "opening_odds": candidate["opening_odds"], "odds_taken": candidate["opening_odds"], "status": "pending"})
                    sb("PATCH", f"virtual_portfolios?id=eq.{portfolio['id']}", {"current_bankroll": round(float(portfolio["current_bankroll"]) - stake, 2), "open_exposure": round(float(portfolio["open_exposure"]) + stake, 2), "updated_at": datetime.now(timezone.utc).isoformat()})
                    log(run_id, "decision", portfolio["id"], "combo_position_created", {"candidates": chosen, "stake": stake, "combined_odds": combined_odds, "combined_edge": combined_edge})
                    created += 1
    return created


@app.get("/api/paper-bot-loop")
def run_loop():
    run_id = datetime.now(timezone.utc).isoformat()
    try:
        settled = settle_positions(run_id)
        filter_id = ensure_filter()
        portfolios = sb("GET", "virtual_portfolios?is_active=eq.true&order=name.asc")
        candidates = get_candidates()
        created = create_positions(run_id, filter_id, portfolios, candidates)
        return jsonify({"ok": True, "disclaimer": DISCLAIMER, "run_id": run_id, "settled": settled, "created": created, "candidate_count": len(candidates)})
    except Exception as exc:
        log(run_id, "error", None, str(exc), {})
        return jsonify({"ok": False, "error": "paper_bot_loop_failed", "details": str(exc), "disclaimer": DISCLAIMER}), 500


if __name__ == "__main__":
    app.run()

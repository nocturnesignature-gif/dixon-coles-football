import os
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, jsonify, request
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("paper-bot-loop")

SUPABASE_URL = os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

ALPHA_MIN_EDGE = 0.045
ALPHA_MAX_PICKS = 5
BETA_MIN_EDGE = 0.035
BETA_MAX_LEGS = 2
BETA_MAX_COMBOS = 3
MAX_DAILY_EXPOSURE = 50.0
UNIT_STAKE = 1.0

app = Flask(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def log_activity(
    activity_type: str,
    status: str,
    message: str,
    prediction_id: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    payload = {
        "activity_type": activity_type,
        "status": status,
        "message": message,
        "prediction_id": prediction_id,
        "details": details or {},
        "created_at": iso(utcnow()),
    }
    try:
        supabase.table("paper_bot_activity_log").insert(payload).execute()
    except Exception as exc:
        logger.error("activity log insert failed: %s", exc)


def fetch_open_positions() -> List[Dict[str, Any]]:
    response = (
        supabase.table("paper_bot_positions")
        .select("*")
        .eq("status", "open")
        .order("placed_at", desc=False)
        .limit(500)
        .execute()
    )
    return response.data or []


def fetch_predictions() -> List[Dict[str, Any]]:
    response = (
        supabase.table("football_predictions")
        .select("*")
        .eq("status", "active")
        .order("created_at", desc=True)
        .limit(500)
        .execute()
    )
    return response.data or []


def fetch_results(event_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    if not event_ids:
        return {}
    response = (
        supabase.table("football_events")
        .select("id,home_score,away_score,status,kickoff_at")
        .in_("id", event_ids)
        .execute()
    )
    return {row["id"]: row for row in (response.data or [])}


def parse_kickoff(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def settle_position(position: Dict[str, Any], event: Dict[str, Any]) -> bool:
    home_score = safe_float(event.get("home_score"))
    away_score = safe_float(event.get("away_score"))
    if home_score is None or away_score is None:
        return False

    legs = position.get("legs") or []
    if not legs:
        return False

    all_won = True
    for leg in legs:
        prediction_id = leg.get("prediction_id")
        if not prediction_id:
            all_won = False
            continue
        response = (
            supabase.table("football_predictions")
            .select("event_id,market,selection")
            .eq("id", prediction_id)
            .maybe_single()
            .execute()
        )
        prediction = response.data
        if not prediction or prediction.get("event_id") != position.get("event_id"):
            all_won = False
            continue

        market = prediction.get("market")
        selection = prediction.get("selection")
        if market == "1x2":
            actual = "HOME" if home_score > away_score else "DRAW" if home_score == away_score else "AWAY"
        elif market == "over_under_2_5":
            actual = "OVER" if home_score + away_score > 2.5 else "UNDER"
        else:
            all_won = False
            continue

        if selection != actual:
            all_won = False

    stake = safe_float(position.get("stake"), 0.0) or 0.0
    odds = safe_float(position.get("odds"), 1.0) or 1.0
    payout = stake * odds if all_won else 0.0
    profit = payout - stake

    supabase.table("paper_bot_positions").update(
        {
            "status": "settled",
            "result": "won" if all_won else "lost",
            "payout": payout,
            "profit": profit,
            "settled_at": iso(utcnow()),
        }
    ).eq("id", position["id"]).execute()

    bankroll_response = (
        supabase.table("paper_bot_bankroll")
        .select("*")
        .eq("strategy", position.get("strategy"))
        .maybe_single()
        .execute()
    )
    bankroll = bankroll_response.data
    if bankroll:
        current = safe_float(bankroll.get("current_balance"), 0.0) or 0.0
        new_balance = current + profit
        supabase.table("paper_bot_bankroll").update(
            {
                "current_balance": new_balance,
                "updated_at": iso(utcnow()),
            }
        ).eq("id", bankroll["id"]).execute()

    log_activity(
        "settlement",
        "success",
        f"Position {position['id']} settled as {'won' if all_won else 'lost'}",
        position.get("prediction_id"),
        {"profit": profit, "payout": payout},
    )
    return True


def settlement_pass() -> int:
    positions = fetch_open_positions()
    if not positions:
        return 0

    event_ids = list({position.get("event_id") for position in positions if position.get("event_id")})
    events = fetch_results(event_ids)
    settled = 0

    for position in positions:
        event = events.get(position.get("event_id"))
        if not event:
            continue
        if event.get("status") not in ("finished", "completed", "FT"):
            continue
        if settle_position(position, event):
            settled += 1

    return settled


def get_bankroll(strategy: str) -> Dict[str, Any]:
    response = (
        supabase.table("paper_bot_bankroll")
        .select("*")
        .eq("strategy", strategy)
        .maybe_single()
        .execute()
    )
    if response.data:
        return response.data

    payload = {
        "strategy": strategy,
        "starting_balance": 100.0,
        "current_balance": 100.0,
        "created_at": iso(utcnow()),
        "updated_at": iso(utcnow()),
    }
    inserted = supabase.table("paper_bot_bankroll").insert(payload).execute()
    return inserted.data[0]


def get_recent_clv(strategy: str, days: int = 14) -> Optional[float]:
    since = iso(utcnow() - timedelta(days=days))
    response = (
        supabase.table("paper_bot_positions")
        .select("clv")
        .eq("strategy", strategy)
        .eq("status", "settled")
        .gte("placed_at", since)
        .not_.is_("clv", "null")
        .limit(100)
        .execute()
    )
    values = [safe_float(row.get("clv")) for row in (response.data or [])]
    values = [value for value in values if value is not None]
    if not values:
        return None
    return sum(values) / len(values)


def exposure_multiplier(clv_average: Optional[float]) -> float:
    if clv_average is None:
        return 1.0
    if clv_average >= 0:
        return 1.0
    if clv_average >= -0.02:
        return 0.75
    if clv_average >= -0.05:
        return 0.5
    return 0.25


def candidate_is_upcoming(prediction: Dict[str, Any], now: datetime) -> Tuple[bool, str]:
    kickoff = parse_kickoff(prediction.get("kickoff_at"))
    if not kickoff:
        return False, "missing_kickoff"
    if kickoff <= now:
        return False, "match_already_started"
    return True, "ok"


def opening_cote_is_valid(prediction: Dict[str, Any]) -> Tuple[bool, str]:
    opening_captured_at = prediction.get("opening_captured_at")
    input_cutoff_at = prediction.get("input_cutoff_at")
    if not opening_captured_at or not input_cutoff_at:
        return False, "missing_cutoff_timestamps"
    try:
        opening = datetime.fromisoformat(str(opening_captured_at).replace("Z", "+00:00"))
        cutoff = datetime.fromisoformat(str(input_cutoff_at).replace("Z", "+00:00"))
    except ValueError:
        return False, "invalid_cutoff_timestamps"
    if opening.tzinfo is None:
        opening = opening.replace(tzinfo=timezone.utc)
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    if opening > cutoff:
        return False, "opening_after_cutoff"
    return True, "ok"


def alpha_candidates(predictions: List[Dict[str, Any]], now: datetime) -> List[Dict[str, Any]]:
    candidates = []
    for prediction in predictions:
        upcoming, reason = candidate_is_upcoming(prediction, now)
        if not upcoming:
            log_activity("candidate_filter", "rejected", reason, prediction.get("id"))
            continue

        valid_opening, opening_reason = opening_cote_is_valid(prediction)
        if not valid_opening:
            log_activity("candidate_filter", "rejected", opening_reason, prediction.get("id"))
            continue

        edge = safe_float(prediction.get("edge"))
        if edge is None or edge < ALPHA_MIN_EDGE:
            continue

        candidates.append(prediction)

    candidates.sort(key=lambda row: safe_float(row.get("edge"), 0.0) or 0.0, reverse=True)
    return candidates[:ALPHA_MAX_PICKS]


def beta_candidates(predictions: List[Dict[str, Any]], now: datetime) -> List[Dict[str, Any]]:
    eligible = []
    for prediction in predictions:
        upcoming, reason = candidate_is_upcoming(prediction, now)
        if not upcoming:
            log_activity("candidate_filter", "rejected", reason, prediction.get("id"))
            continue

        valid_opening, opening_reason = opening_cote_is_valid(prediction)
        if not valid_opening:
            log_activity("candidate_filter", "rejected", opening_reason, prediction.get("id"))
            continue

        edge = safe_float(prediction.get("edge"))
        if edge is None or edge < BETA_MIN_EDGE:
            continue

        eligible.append(prediction)

    eligible.sort(key=lambda row: safe_float(row.get("edge"), 0.0) or 0.0, reverse=True)

    combos = []
    used_events = set()
    for first in eligible:
        if len(combos) >= BETA_MAX_COMBOS:
            break
        if first["event_id"] in used_events:
            continue
        for second in eligible:
            if second["id"] == first["id"]:
                continue
            if second["event_id"] == first["event_id"]:
                continue
            if second["event_id"] in used_events:
                continue
            combos.append([first, second])
            used_events.add(first["event_id"])
            used_events.add(second["event_id"])
            break

    return combos


def place_alpha_bet(prediction: Dict[str, Any], multiplier: float) -> bool:
    strategy = "alpha"
    bankroll = get_bankroll(strategy)
    balance = safe_float(bankroll.get("current_balance"), 0.0) or 0.0
    stake = round(UNIT_STAKE * multiplier, 2)
    if stake <= 0 or balance < stake:
        log_activity("bet_placement", "rejected", "insufficient_balance", prediction.get("id"))
        return False

    odds = safe_float(prediction.get("odds"))
    if not odds or odds <= 1.0:
        return False

    payload = {
        "strategy": strategy,
        "prediction_id": prediction["id"],
        "event_id": prediction["event_id"],
        "stake": stake,
        "odds": odds,
        "status": "open",
        "placed_at": iso(utcnow()),
        "legs": [{"prediction_id": prediction["id"]}],
    }
    supabase.table("paper_bot_positions").insert(payload).execute()
    supabase.table("paper_bot_bankroll").update(
        {
            "current_balance": balance - stake,
            "updated_at": iso(utcnow()),
        }
    ).eq("id", bankroll["id"]).execute()

    log_activity("bet_placement", "success", "Alpha single placed", prediction.get("id"), {"stake": stake})
    return True


def place_beta_bet(legs: List[Dict[str, Any]], multiplier: float) -> bool:
    strategy = "beta"
    bankroll = get_bankroll(strategy)
    balance = safe_float(bankroll.get("current_balance"), 0.0) or 0.0
    stake = round(UNIT_STAKE * multiplier, 2)
    if stake <= 0 or balance < stake:
        log_activity("bet_placement", "rejected", "insufficient_balance")
        return False

    combined_odds = 1.0
    for leg in legs:
        odds = safe_float(leg.get("odds"))
        if not odds or odds <= 1.0:
            return False
        combined_odds *= odds

    payload = {
        "strategy": strategy,
        "prediction_id": legs[0]["id"],
        "event_id": legs[0]["event_id"],
        "stake": stake,
        "odds": combined_odds,
        "status": "open",
        "placed_at": iso(utcnow()),
        "legs": [{"prediction_id": leg["id"]} for leg in legs],
    }
    supabase.table("paper_bot_positions").insert(payload).execute()
    supabase.table("paper_bot_bankroll").update(
        {
            "current_balance": balance - stake,
            "updated_at": iso(utcnow()),
        }
    ).eq("id", bankroll["id"]).execute()

    log_activity("bet_placement", "success", "Beta combo placed", legs[0]["id"], {"stake": stake, "legs": len(legs)})
    return True


def run() -> Dict[str, Any]:
    now = utcnow()
    settled = settlement_pass()
    predictions = fetch_predictions()

    alpha = alpha_candidates(predictions, now)
    beta = beta_candidates(predictions, now)

    alpha_clv = get_recent_clv("alpha")
    beta_clv = get_recent_clv("beta")
    alpha_multiplier = exposure_multiplier(alpha_clv)
    beta_multiplier = exposure_multiplier(beta_clv)

    alpha_placed = 0
    beta_placed = 0

    for prediction in alpha:
        if alpha_placed >= ALPHA_MAX_PICKS:
            break
        if place_alpha_bet(prediction, alpha_multiplier):
            alpha_placed += 1

    for legs in beta:
        if beta_placed >= BETA_MAX_COMBOS:
            break
        if place_beta_bet(legs, beta_multiplier):
            beta_placed += 1

    summary = {
        "settled": settled,
        "alpha_candidates": len(alpha),
        "beta_candidates": len(beta),
        "alpha_placed": alpha_placed,
        "beta_placed": beta_placed,
        "alpha_clv": alpha_clv,
        "beta_clv": beta_clv,
        "alpha_multiplier": alpha_multiplier,
        "beta_multiplier": beta_multiplier,
    }

    log_activity("loop_summary", "success", "Paper bot loop completed", details=summary)
    return summary


@app.get("/api/paper-bot-loop")
def paper_bot_loop_endpoint():
    secret = os.environ.get("SYNC_SECRET", "")
    provided = request.headers.get("x-sync-secret", "")

    if not secret or provided != secret:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    try:
        result = run()
        return jsonify({"ok": True, "result": result})
    except Exception as exc:
        logger.exception("paper bot loop failed")
        log_activity("loop_summary", "error", str(exc), details={"error_type": type(exc).__name__})
        return jsonify({"ok": False, "error": "paper_bot_loop_failed", "details": str(exc)}), 500


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, default=str))
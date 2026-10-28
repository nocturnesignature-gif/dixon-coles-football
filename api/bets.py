"""Bet journal API with closing-line value tracking."""

import json
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone
from flask import Flask, jsonify, request

app = Flask(__name__)


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
    api_request = urllib.request.Request(
        f"{base_url}/rest/v1/{path}",
        method=method,
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        },
        data=json.dumps(payload).encode() if payload is not None else None,
    )
    try:
        with urllib.request.urlopen(api_request, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise SupabaseError(exc.code, exc.read().decode(errors="replace")[:500]) from exc


def closing_line_value(placed_odds, closing_odds):
    if not closing_odds or closing_odds <= 1:
        return None
    return placed_odds / closing_odds - 1


def validate_bet(payload):
    required = [
        "home_team", "away_team", "market", "outcome",
        "model_probability", "placed_odds", "expected_value",
        "stake_fraction",
    ]
    missing = [field for field in required if payload.get(field) is None]
    if missing:
        raise ValueError(f"missing_fields:{','.join(missing)}")
    if not 0 <= float(payload["model_probability"]) <= 1:
        raise ValueError("invalid_model_probability")
    if float(payload["placed_odds"]) <= 1:
        raise ValueError("invalid_placed_odds")
    if float(payload["stake_fraction"]) < 0:
        raise ValueError("invalid_stake_fraction")


@app.get("/api/bets")
def list_bets():
    try:
        bets = supabase_request(
            "bet_history?select=*&order=created_at.desc&limit=200"
        )
        settled = [b for b in bets if b.get("result") in ("won", "lost", "void")]
        clv_values = [float(b["closing_line_value"]) for b in settled if b.get("closing_line_value") is not None]
        profit_values = [float(b["profit"]) for b in settled if b.get("profit") is not None]
        return jsonify({
            "ok": True,
            "bets": bets,
            "summary": {
                "total": len(bets),
                "settled": len(settled),
                "average_clv": sum(clv_values) / len(clv_values) if clv_values else None,
                "total_profit": sum(profit_values) if profit_values else 0,
            },
        })
    except SupabaseError as exc:
        return jsonify({"ok": False, "error": f"supabase_http_{exc.status}", "details": exc.body}), 502
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/bets")
def create_bet():
    payload = request.get_json(silent=True) or {}
    try:
        validate_bet(payload)
        row = {
            "event_id": payload.get("event_id"),
            "competition": payload.get("competition"),
            "home_team": payload["home_team"],
            "away_team": payload["away_team"],
            "kickoff_at": payload.get("kickoff_at"),
            "market": payload["market"],
            "outcome": payload["outcome"],
            "model_probability": payload["model_probability"],
            "placed_odds": payload["placed_odds"],
            "expected_value": payload["expected_value"],
            "stake_fraction": payload["stake_fraction"],
            "stake_amount": payload.get("stake_amount"),
            "currency": payload.get("currency", "EUR"),
            "profile": payload.get("profile", "balanced"),
            "result": "pending",
        }
        created = supabase_request("bet_history", method="POST", payload=row)
        return jsonify({"ok": True, "bet": created[0] if created else row}), 201
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except SupabaseError as exc:
        return jsonify({"ok": False, "error": f"supabase_http_{exc.status}", "details": exc.body}), 502
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/bets/<bet_id>/settle")
def settle_bet(bet_id):
    payload = request.get_json(silent=True) or {}
    result = payload.get("result")
    closing_odds = payload.get("closing_odds")
    if result not in ("won", "lost", "void"):
        return jsonify({"ok": False, "error": "invalid_result"}), 400
    try:
        existing = supabase_request(f"bet_history?id=eq.{bet_id}&select=*")
        if not existing:
            return jsonify({"ok": False, "error": "bet_not_found"}), 404
        bet = existing[0]
        clv = closing_line_value(float(bet["placed_odds"]), closing_odds)
        stake = float(bet.get("stake_amount") or 0)
        if result == "won":
            profit = stake * (float(bet["placed_odds"]) - 1)
        elif result == "lost":
            profit = -stake
        else:
            profit = 0
        updated = supabase_request(
            f"bet_history?id=eq.{bet_id}",
            method="PATCH",
            payload={
                "result": result,
                "closing_odds": closing_odds,
                "closing_line_value": clv,
                "profit": profit,
                "settled_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return jsonify({"ok": True, "bet": updated[0] if updated else None})
    except SupabaseError as exc:
        return jsonify({"ok": False, "error": f"supabase_http_{exc.status}", "details": exc.body}), 502
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

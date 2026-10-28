"""Capture closing odds for pending bets shortly before kickoff.

Uses the latest available odds snapshot as the closing reference. The CLV is
computed only when both the placed odds and a closing reference exist.
"""

import json
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify

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
    request = urllib.request.Request(
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
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise SupabaseError(exc.code, exc.read().decode(errors="replace")[:500]) from exc


def parse_datetime(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@app.get("/api/capture-closing-odds")
def capture_closing_odds():
    try:
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(minutes=20)
        pending = supabase_request(
            "bet_history?select=id,event_id,market,placed_odds,kickoff_at"
            "&result=eq.pending&kickoff_at=gte." + now.isoformat() +
            "&kickoff_at=lt." + horizon.isoformat()
        )
        if not pending:
            return jsonify({"ok": True, "bets_processed": 0, "closing_odds_captured": 0})

        odds = supabase_request(
            "live_odds?select=event_id,market,odds,captured_at"
            "&order=captured_at.desc&limit=1000"
        )
        latest = {}
        for row in odds:
            latest.setdefault((row["event_id"], row["market"]), row)

        captured = 0
        for bet in pending:
            reference = latest.get((bet.get("event_id"), bet["market"]))
            if not reference:
                continue
            closing_odds = reference["odds"]
            placed_odds = float(bet["placed_odds"])
            clv = placed_odds / closing_odds - 1 if closing_odds > 1 else None
            supabase_request(
                f"bet_history?id=eq.{bet['id']}",
                method="PATCH",
                payload={
                    "closing_odds": closing_odds,
                    "closing_line_value": clv,
                },
            )
            captured += 1

        return jsonify({
            "ok": True,
            "bets_processed": len(pending),
            "closing_odds_captured": captured,
        })
    except SupabaseError as exc:
        return jsonify({"ok": False, "error": f"supabase_http_{exc.status}", "details": exc.body}), 502
    except (RuntimeError, urllib.error.URLError) as exc:
        return jsonify({"ok": False, "error": "closing_odds_capture_failed"}), 500

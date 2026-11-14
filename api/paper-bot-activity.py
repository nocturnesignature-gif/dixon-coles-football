"""Paper Bot activity stream: read-only journal of autonomous decisions."""
import json
import os
import urllib.parse
import urllib.request
from flask import Flask, jsonify

app = Flask(__name__)
DISCLAIMER = "Estimations expérimentales — calibration prospective non validée. Aucune recommandation de pari."


def supabase_get(path):
    base = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base or not key:
        raise RuntimeError("missing_supabase_config")
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"}
    req = urllib.request.Request(f"{base}/rest/v1/{path}", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def format_event(event):
    action = event.get("action")
    reason = event.get("reason") or ""
    details = event.get("details") or {}
    if action == "decision":
        if details.get("candidate"):
            candidate = details["candidate"]
            message = f"Position créée : {candidate.get('home_team')} vs {candidate.get('away_team')} — {candidate.get('selection')} à {candidate.get('opening_odds')} — edge {float(candidate.get('edge', 0)) * 100:.2f}% — mise {details.get('stake')} €"
        else:
            message = f"Position créée : {reason}"
    elif action == "rejection":
        message = f"Aucune nouvelle position : {reason}"
    elif action == "settlement":
        message = f"Règlement : {reason} — P&L {details.get('pnl')} €"
    elif action == "error":
        message = f"Erreur : {reason}"
    else:
        message = reason or action
    return {
        "id": event.get("id"),
        "created_at": event.get("created_at"),
        "action": action,
        "portfolio_id": event.get("portfolio_id"),
        "competition": event.get("competition"),
        "market": event.get("market"),
        "selection": event.get("selection"),
        "edge": event.get("edge"),
        "stake_amount": event.get("stake_amount"),
        "status": event.get("status"),
        "message": message,
        "details": details,
    }


@app.get("/api/paper-bot/activity")
def activity():
    try:
        limit = min(200, max(1, int(urllib.parse.parse_qs(urllib.parse.urlparse(request.url).query).get("limit", ["100"])[0])))
        rows = supabase_get(f"paper_bot_activity_log?order=created_at.desc&limit={limit}")
        return jsonify({"ok": True, "disclaimer": DISCLAIMER, "events": [format_event(row) for row in rows]})
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc), "disclaimer": DISCLAIMER}), 500
    except Exception as exc:
        return jsonify({"ok": False, "error": "activity_stream_failed", "details": str(exc), "disclaimer": DISCLAIMER}), 500


from flask import request

if __name__ == "__main__":
    app.run()

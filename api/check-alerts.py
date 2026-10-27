import json
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone
from flask import Flask, jsonify

app = Flask(__name__)

LEAGUES = ["ligue_1", "premier_league", "la_liga", "bundesliga", "serie_a"]
ALERT_EDGE = 0.15
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "PF-7f3k9-valeur")


class SupabaseError(Exception):
    def __init__(self, status, url, body):
        super().__init__(f"Supabase HTTP {status}")
        self.status = status
        self.url = url
        self.body = body


def supabase_request(path, method="GET", payload=None):
    base_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base_url or not service_key:
        raise RuntimeError("missing_supabase_config")
    url = f"{base_url}/rest/v1/{path}"
    request = urllib.request.Request(
        url,
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
        body = exc.read().decode(errors="replace")[:500]
        raise SupabaseError(exc.code, url, body) from exc


def optional_supabase_request(path, method="GET", payload=None):
    try:
        return supabase_request(path, method=method, payload=payload), True
    except SupabaseError as exc:
        if exc.status == 404:
            return [], False
        raise


def send_ntfy(message, title, priority="default", tags=None):
    payload = {
        "topic": NTFY_TOPIC,
        "title": title,
        "message": message,
        "priority": priority,
        "tags": tags or ["soccer"],
    }
    request = urllib.request.Request(
        "https://ntfy.sh/",
        method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload).encode(),
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.status


def kelly_fraction(expected_value, odds):
    if odds <= 1 or expected_value <= 0:
        return 0
    return min(0.02, 0.25 * expected_value / (odds - 1))


@app.get("/api/check-alerts")
def check_alerts():
    try:
        runs = supabase_request(
            "football_model_runs_v4?select=id,model_version,competition,status,created_at"
            "&competition=in.(" + ",".join(LEAGUES) + ")"
            "&order=created_at.desc&limit=100"
        )
        latest = {}
        for run in runs:
            latest.setdefault(run["competition"], run)
        versions = [run["model_version"] for run in latest.values()]
        if not versions:
            return jsonify({"ok": True, "alerts_sent": 0, "reason": "no_model_runs"})
        forecasts = supabase_request(
            "football_experimental_forecasts_v4?select=*"
            "&model_version=in.(" + ",".join(versions) + ")"
            "&order=kickoff_at.asc"
        )
        odds = supabase_request(
            "live_odds?select=event_id,market,odds,provider,captured_at"
            "&order=captured_at.desc&limit=1000"
        )
        latest_odds = {}
        for row in odds:
            latest_odds.setdefault((row["event_id"], row["market"]), row)
        existing, alerts_table_available = optional_supabase_request(
            "bet_alerts?select=event_id,market&order=created_at.desc&limit=1000"
        )
        already_alerted = {(row["event_id"], row["market"]) for row in existing}
        alerts = []
        seen_in_run = set()
        for forecast in forecasts:
            event_id = forecast.get("external_id")
            probabilities = {
                "home": forecast["home_probability"],
                "draw": forecast["draw_probability"],
                "away": forecast["away_probability"],
            }
            for market, outcome in (
                ("home", "Domicile"),
                ("draw", "Nul"),
                ("away", "Extérieur"),
            ):
                odd = latest_odds.get((event_id, market))
                if not odd:
                    continue
                probability = probabilities[market]
                expected_value = probability * odd["odds"] - 1
                if expected_value < ALERT_EDGE:
                    continue
                key = (event_id, market)
                if key in already_alerted or key in seen_in_run:
                    continue
                seen_in_run.add(key)
                alerts.append({
                    "event_id": event_id,
                    "market": market,
                    "competition": forecast["competition"],
                    "home_team": forecast["home_team"],
                    "away_team": forecast["away_team"],
                    "outcome": outcome,
                    "model_probability": probability,
                    "odds": odd["odds"],
                    "expected_value": expected_value,
                    "suggested_stake_fraction": kelly_fraction(expected_value, odd["odds"]),
                })
        alerts.sort(key=lambda x: x["expected_value"], reverse=True)
        sent = 0
        persistence_errors = 0
        for alert in alerts:
            message = (
                f"{alert['home_team']} — {alert['away_team']}\n"
                f"Pari : {alert['outcome']} @ {alert['odds']:.2f}\n"
                f"Probabilité modèle : {alert['model_probability'] * 100:.1f}%\n"
                f"Valeur attendue : +{alert['expected_value'] * 100:.1f}%\n"
                f"Mise indicative : {alert['suggested_stake_fraction'] * 100:.2f}% de cagnotte"
            )
            priority = "high" if alert["expected_value"] >= 0.25 else "default"
            send_ntfy(message, "Très bon pari détecté", priority=priority, tags=["soccer", "moneybag"])
            if alerts_table_available:
                try:
                    supabase_request("bet_alerts", method="POST", payload=[{
                        "event_id": alert["event_id"],
                        "market": alert["market"],
                        "competition": alert["competition"],
                        "home_team": alert["home_team"],
                        "away_team": alert["away_team"],
                        "outcome": alert["outcome"],
                        "model_probability": alert["model_probability"],
                        "odds": alert["odds"],
                        "expected_value": alert["expected_value"],
                        "suggested_stake_fraction": alert["suggested_stake_fraction"],
                        "notified_at": datetime.now(timezone.utc).isoformat(),
                    }])
                except SupabaseError:
                    persistence_errors += 1
            sent += 1
        return jsonify({
            "ok": True,
            "alerts_found": len(alerts),
            "alerts_sent": sent,
            "topic": NTFY_TOPIC,
            "alerts_table_available": alerts_table_available,
            "persistence_errors": persistence_errors,
        })
    except SupabaseError as exc:
        return jsonify({
            "ok": False,
            "error": f"supabase_http_{exc.status}",
            "url": exc.url,
            "details": exc.body,
        }), 502
    except urllib.error.HTTPError as exc:
        return jsonify({"ok": False, "error": f"upstream_http_{exc.code}"}), 502
    except (RuntimeError, urllib.error.URLError) as exc:
        return jsonify({"ok": False, "error": "alert_check_failed"}), 500

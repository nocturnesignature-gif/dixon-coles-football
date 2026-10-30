"""Vercel route to backfill, train and generate forecasts for the three newly added leagues."""
import json
import os
import urllib.parse
import urllib.request
import urllib.error
from flask import Flask, jsonify

app = Flask(__name__)


def _request(url, method="GET", headers=None, payload=None, timeout=120):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.status, json.load(response)


@app.get("/api/train-new-leagues")
def train_new_leagues():
    base_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    token = os.environ.get("SUPABASE_FORECAST_TOKEN", "")
    secret = os.environ.get("SYNC_SECRET", "")
    if not base_url or not token or not secret:
        return jsonify({"ok": False, "error": "missing_training_config"}), 500
    try:
        backfill_url = f"{base_url}/functions/v1/backfill-multi-league-history-v1?token={urllib.parse.quote(token)}"
        backfill_status, backfill = _request(backfill_url)
        if backfill_status >= 400 or not backfill.get("success"):
            return jsonify({"ok": False, "stage": "backfill", "backfill": backfill}), 502
        train_url = f"{base_url}/functions/v1/train-dixon-coles-candidates-v2"
        train_status, training = _request(
            train_url,
            method="POST",
            headers={"x-sync-secret": secret, "Content-Type": "application/json"},
            payload={},
        )
        if train_status >= 400 or not training.get("success"):
            return jsonify({"ok": False, "stage": "training", "backfill": backfill, "training": training}), 502
        forecast_url = f"{base_url}/functions/v1/football-model-prospective-multi-league-v1?token={urllib.parse.quote(token)}"
        forecast_status, forecasts = _request(forecast_url)
        return jsonify({
            "ok": forecast_status < 400,
            "backfill": backfill,
            "training": training,
            "forecasts": forecasts,
        })
    except urllib.error.HTTPError as exc:
        return jsonify({"ok": False, "error": f"upstream_http_{exc.code}", "details": exc.read().decode(errors="replace")[:500]}), 502
    except (RuntimeError, urllib.error.URLError) as exc:
        return jsonify({"ok": False, "error": "training_pipeline_failed", "details": str(exc)}), 500

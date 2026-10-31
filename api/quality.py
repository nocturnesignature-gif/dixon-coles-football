"""Read-only prediction quality endpoint; no betting or model activation."""
import json
import os
import urllib.error
import urllib.request
from flask import Flask, jsonify

app = Flask(__name__)


def read_view(name):
    base = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base or not key:
        raise RuntimeError("missing_supabase_config")
    req = urllib.request.Request(
        f"{base}/rest/v1/{name}?select=*",
        headers={"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


@app.after_request
def no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/prediction-quality")
@app.get("/api/quality")
def quality():
    try:
        counts = read_view("football_quality_counts_v1")
        summaries = read_view("football_quality_summary_v1")
        return jsonify({
            "ok": True, "status": "experimental_unvalidated",
            "counts": counts[0] if counts else {},
            "summary": summaries,
            "selection_policy": "earliest_recorded_pre_kickoff_forecast_v1",
            "probabilities": "raw",
            "limitations": [
                "Overall and competition summaries retain the earliest recorded valid pre-kickoff forecast per event.",
                "Model summaries retain one forecast per event and model version; model sample sizes can differ.",
                "Historical baseline uses only prior results observed by prediction time, with add-one smoothing.",
                "Market baseline requires a complete same-provider same-time 1X2 snapshot, captured and stored before prediction, at most six hours old.",
                "Compare model and baseline only on their matched subsets; null means unavailable, not zero.",
                "Historical availability and fixture provenance remain subject to the upstream data quality limitations.",
                "This report does not validate a model or demonstrate betting profitability.",
            ],
        })
    except RuntimeError:
        return jsonify({"ok": False, "error": "missing_supabase_config"}), 500
    except urllib.error.HTTPError as exc:
        return jsonify({"ok": False, "error": "quality_upstream_http_error", "upstream_status": exc.code}), 502
    except (urllib.error.URLError, TimeoutError, ValueError):
        return jsonify({"ok": False, "error": "quality_upstream_unavailable"}), 502

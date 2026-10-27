import json
import os
import urllib.request
import urllib.error
from flask import Flask, jsonify

app = Flask(__name__)

FORECAST_VIEW = "football_experimental_forecasts_v4"
RESULTS_VIEW = "football_model_prospective_results_v4"


def supabase_get(view: str):
    base_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base_url or not service_key:
        raise RuntimeError("Missing Supabase server environment configuration")
    url = f"{base_url}/rest/v1/{view}?select=*&order=kickoff_at.asc"
    request = urllib.request.Request(
        url,
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        if response.status != 200:
            raise RuntimeError(f"Supabase returned HTTP {response.status}")
        return json.load(response)


@app.get("/api/forecasts")
def forecasts():
    try:
        return jsonify(
            {
                "ok": True,
                "status": "experimental_unvalidated",
                "forecasts": supabase_get(FORECAST_VIEW),
                "results": supabase_get(RESULTS_VIEW),
            }
        )
    except urllib.error.HTTPError as exc:
        return jsonify({"ok": False, "error": f"upstream_http_{exc.code}"}), 502
    except (RuntimeError, urllib.error.URLError) as exc:
        return jsonify({"ok": False, "error": "configuration_or_upstream_error"}), 500

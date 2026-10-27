import json
import os
import urllib.request
import urllib.error
from flask import Flask, jsonify

app = Flask(__name__)

RESULTS_VIEW = "football_model_prospective_results_v4"


def supabase_get(path: str):
    base_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base_url or not service_key:
        raise RuntimeError("Missing Supabase server environment configuration")
    url = f"{base_url}/rest/v1/{path}"
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
        runs = supabase_get(
            "football_model_runs_v4?select=id,model_version,status,created_at"
            "&competition=eq.ligue_1&order=created_at.desc&limit=1"
        )
        if not runs:
            return jsonify({"ok": True, "status": "no_model", "forecasts": [], "results": []})
        model = runs[0]
        forecasts = supabase_get(
            "football_experimental_forecasts_v4?select=*"
            f"&model_version=eq.{model['model_version']}"
            "&order=kickoff_at.asc"
        )
        results = supabase_get(f"{RESULTS_VIEW}?select=*&order=kickoff_at.asc")
        return jsonify(
            {
                "ok": True,
                "status": "experimental_unvalidated",
                "model": {
                    "id": model["id"],
                    "version": model["model_version"],
                    "status": model["status"],
                },
                "forecasts": forecasts,
                "results": results,
            }
        )
    except urllib.error.HTTPError as exc:
        return jsonify({"ok": False, "error": f"upstream_http_{exc.code}"}), 502
    except (RuntimeError, urllib.error.URLError) as exc:
        return jsonify({"ok": False, "error": "configuration_or_upstream_error"}), 500

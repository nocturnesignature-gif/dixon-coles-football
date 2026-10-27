import json
import os
import urllib.request
import urllib.error
from flask import Flask, jsonify

app = Flask(__name__)

LEAGUES = ["ligue_1", "premier_league", "la_liga", "bundesliga", "serie_a"]
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
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise RuntimeError(f"Supabase returned HTTP {response.status}")
        return json.load(response)


@app.get("/api/forecasts")
def forecasts():
    try:
        runs = supabase_get(
            "football_model_runs_v4?select=id,model_version,competition,status,created_at"
            "&competition=in.(" + ",".join(LEAGUES) + ")"
            "&order=created_at.desc&limit=100"
        )
        latest = {}
        for run in runs:
            latest.setdefault(run["competition"], run)
        versions = [run["model_version"] for run in latest.values()]
        forecasts = supabase_get(
            "football_experimental_forecasts_v4?select=*"
            "&model_version=in.(" + ",".join(versions) + ")"
            "&order=kickoff_at.asc"
        ) if versions else []
        by_version = {}
        for forecast in forecasts:
            by_version.setdefault(forecast["model_version"], []).append(forecast)
        leagues = []
        for competition in LEAGUES:
            run = latest.get(competition)
            if not run:
                continue
            leagues.append({
                "competition": competition,
                "model": {
                    "id": run["id"],
                    "version": run["model_version"],
                    "status": run["status"],
                },
                "forecasts": by_version.get(run["model_version"], []),
            })
        results = supabase_get(f"{RESULTS_VIEW}?select=*&order=kickoff_at.asc")
        return jsonify({
            "ok": True,
            "status": "experimental_unvalidated",
            "leagues": leagues,
            "results": results,
        })
    except urllib.error.HTTPError as exc:
        return jsonify({"ok": False, "error": f"upstream_http_{exc.code}"}), 502
    except (RuntimeError, urllib.error.URLError) as exc:
        return jsonify({"ok": False, "error": "configuration_or_upstream_error"}), 500

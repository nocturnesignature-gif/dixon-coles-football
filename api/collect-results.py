import os
import urllib.request
import urllib.error
from flask import Flask, jsonify

app = Flask(__name__)


def supabase_function_url():
    base_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if not base_url:
        raise RuntimeError("Missing Supabase URL")
    return f"{base_url}/functions/v1/football-data-status-ingest-v4"


@app.get("/api/collect-results")
def collect_results():
    try:
        service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        if not service_key:
            return jsonify({"ok": False, "error": "missing_service_key"}), 500
        request = urllib.request.Request(
            supabase_function_url(),
            method="POST",
            headers={
                "Authorization": f"Bearer {service_key}",
                "Content-Type": "application/json",
            },
            data=b"{}",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
            status = response.status
        return app.response_class(body, status=status, mimetype="application/json")
    except urllib.error.HTTPError as exc:
        return jsonify({"ok": False, "error": f"upstream_http_{exc.code}"}), 502
    except (RuntimeError, urllib.error.URLError) as exc:
        return jsonify({"ok": False, "error": "collection_failed"}), 500

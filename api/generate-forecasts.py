"""Authenticated bridge to prospective generation; dry run by default."""
import hmac
import json
import os
import urllib.request
import urllib.error
from flask import Flask, jsonify, request

app = Flask(__name__)

@app.after_request
def no_store(response):
    response.headers['Cache-Control'] = 'no-store'
    return response

@app.route('/api/generate-forecasts', methods=['GET', 'POST'])
def generate():
    secret = os.environ.get('SYNC_SECRET', '')
    cron = os.environ.get('CRON_SECRET', '')
    provided = request.headers.get('Authorization', '')
    supplied = request.headers.get('x-sync-secret', '')
    authorized = bool((cron and hmac.compare_digest(provided, 'Bearer '+cron)) or (secret and hmac.compare_digest(supplied, secret)))
    if not authorized:
        return jsonify(ok=False, error='unauthorized'), 401
    base = os.environ.get('SUPABASE_URL', '').rstrip('/')
    if not base.startswith('https://') or not secret:
        return jsonify(ok=False, error='missing_prospective_config'), 503
    try:
        models = json.loads(os.environ.get('PROSPECTIVE_MODEL_IDS_JSON', '{}'))
    except ValueError:
        return jsonify(ok=False, error='invalid_model_configuration'), 503
    if not isinstance(models, dict) or not models or not all(isinstance(k, str) and isinstance(v, str) for k,v in models.items()):
        return jsonify(ok=False, error='explicit_model_configuration_required'), 503
    body = request.get_json(silent=True) if request.method == 'POST' else {}
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return jsonify(ok=False, error='invalid_payload'), 400
    persist = body.get('persist') is True
    if persist and os.environ.get('PROSPECTIVE_WRITES_ENABLED', '').lower() != 'true':
        return jsonify(ok=False, error='experimental_writes_not_enabled'), 403
    payload = {'model_ids': models, 'persist': persist}
    req = urllib.request.Request(base+'/functions/v1/football-model-prospective-multi-league-v1', method='POST', headers={'x-sync-secret':secret,'Content-Type':'application/json','Accept':'application/json'}, data=json.dumps(payload).encode())
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            result = json.load(response)
        return jsonify(result)
    except urllib.error.HTTPError as exc:
        app.logger.warning('Prospective upstream HTTP %s', exc.code)
        return jsonify(ok=False, error='prospective_upstream_http_'+str(exc.code)), 502
    except (urllib.error.URLError, TimeoutError, ValueError):
        return jsonify(ok=False, error='prospective_upstream_unavailable'), 502

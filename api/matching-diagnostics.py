"""Read-only diagnostics for market and closing-odds matching failures."""
import json
import os
import urllib.error
import urllib.request
from collections import Counter
from flask import Flask, jsonify

app = Flask(__name__)


def read_view(name, order):
    base = os.environ.get('SUPABASE_URL', '').rstrip('/')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
    if not base or not key:
        raise RuntimeError('missing_supabase_config')
    rows = []
    for offset in range(0, 100000, 1000):
        url = f'{base}/rest/v1/{name}?select=*&order={order}&limit=1000&offset={offset}'
        req = urllib.request.Request(url, headers={'apikey': key, 'Authorization': f'Bearer {key}', 'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=30) as response:
            page = json.load(response)
        if not isinstance(page, list):
            raise ValueError('invalid_upstream_payload')
        rows.extend(page)
        if len(page) < 1000:
            return rows
    raise ValueError('matching_diagnostics_scan_limit_reached')


def market_status(row):
    if row.get('market_log_loss') is None or row.get('market_brier_score') is None:
        return 'market_reference_missing'
    if not row.get('market_provider'):
        return 'market_provider_missing'
    return 'market_reference_matched'


def closing_status(row):
    if 'closing_log_loss' not in row or 'closing_brier_score' not in row:
        return 'closing_fields_unavailable'
    if row.get('closing_log_loss') is None or row.get('closing_brier_score') is None:
        return 'closing_reference_missing'
    return 'closing_reference_matched'


@app.after_request
def no_store(response):
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.get('/api/matching-diagnostics')
def matching_diagnostics():
    try:
        samples = read_view('football_quality_samples_with_closing_v1', 'forecast_id.asc')
        market_counts = Counter()
        closing_counts = Counter()
        by_competition = {}
        for row in samples:
            competition = row.get('competition') or 'unknown'
            market = market_status(row)
            closing = closing_status(row)
            market_counts[market] += 1
            closing_counts[closing] += 1
            bucket = by_competition.setdefault(competition, {
                'distinct_matches': 0,
                'market_reference_matched': 0,
                'market_reference_missing': 0,
                'closing_reference_matched': 0,
                'closing_reference_missing': 0,
                'closing_fields_unavailable': 0,
            })
            bucket['distinct_matches'] += 1
            if market == 'market_reference_matched':
                bucket['market_reference_matched'] += 1
            else:
                bucket['market_reference_missing'] += 1
            bucket[closing] += 1
        return jsonify(
            ok=True,
            status='experimental_unvalidated',
            calibration_validated=False,
            candidate_generation_enabled=False,
            paper_trading_enabled=False,
            totals={
                'distinct_matches': len(samples),
                'market_reference_matched': market_counts['market_reference_matched'],
                'market_reference_missing': market_counts['market_reference_missing'],
                'market_provider_missing': market_counts['market_provider_missing'],
                'closing_reference_matched': closing_counts['closing_reference_matched'],
                'closing_reference_missing': closing_counts['closing_reference_missing'],
                'closing_fields_unavailable': closing_counts['closing_fields_unavailable'],
            },
            market_reasons=dict(market_counts),
            closing_reasons=dict(closing_counts),
            by_competition=by_competition,
            limitations=[
                'Read-only diagnostics; no candidates, positions, bankroll changes or model activation are performed.',
                'Market matching uses the deduplicated quality sample and its stored market-reference fields.',
                'Closing matching uses the latest complete same-provider 1X2 snapshot captured after the entry reference and before kickoff.',
                'Missing references are never imputed or reconstructed after the result.'
            ]
        )
    except RuntimeError:
        return jsonify(ok=False, error='missing_supabase_config'), 500
    except urllib.error.HTTPError as exc:
        return jsonify(ok=False, error='matching_diagnostics_upstream_http_error', upstream_status=exc.code), 502
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, TypeError):
        return jsonify(ok=False, error='matching_diagnostics_upstream_unavailable'), 502

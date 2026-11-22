"""Read-only shadow comparison between stored model versions; never activates betting."""
import json
import os
import urllib.error
import urllib.request
from statistics import mean
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
    raise ValueError('shadow_comparison_scan_limit_reached')


def metric(rows, field):
    values = [float(row[field]) for row in rows if row.get(field) is not None]
    return mean(values) if values else None


def probability_dispersion(rows):
    keys = [
        ('home_win_probability', 'draw_probability', 'away_win_probability'),
        ('home_win', 'draw', 'away_win'),
    ]
    selected = None
    for candidate in keys:
        if rows and all(any(key in row for row in rows) for key in candidate):
            selected = candidate
            break
    if not selected:
        return {'available': False, 'reason': 'probability_fields_unavailable'}
    result = {}
    for label, key in zip(('home_win', 'draw', 'away_win'), selected):
        values = [float(row[key]) for row in rows if row.get(key) is not None]
        result[label] = {
            'mean': mean(values) if values else None,
            'min': min(values) if values else None,
            'max': max(values) if values else None,
            'range': (max(values) - min(values)) if values else None,
        }
    return {'available': True, 'metrics': result}


@app.after_request
def no_store(response):
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.get('/api/model-shadow-comparison')
def model_shadow_comparison():
    try:
        raw = read_view('football_quality_samples_v1', 'forecast_id.asc')
        by_event = {}
        for row in raw:
            by_event.setdefault(row['event_id'], []).append(row)
        versions = sorted({row.get('model_version') or 'unknown' for row in raw})
        comparisons = []
        for index, left in enumerate(versions):
            for right in versions[index + 1:]:
                left_rows = []
                right_rows = []
                for event_rows in by_event.values():
                    left_match = [row for row in event_rows if (row.get('model_version') or 'unknown') == left]
                    right_match = [row for row in event_rows if (row.get('model_version') or 'unknown') == right]
                    if left_match and right_match:
                        left_rows.append(left_match[-1])
                        right_rows.append(right_match[-1])
                if not left_rows:
                    continue
                comparisons.append({
                    'left_model_version': left,
                    'right_model_version': right,
                    'shared_matches': len(left_rows),
                    'left_brier_score': metric(left_rows, 'brier_score'),
                    'right_brier_score': metric(right_rows, 'brier_score'),
                    'brier_delta_right_minus_left': (metric(right_rows, 'brier_score') - metric(left_rows, 'brier_score')) if metric(right_rows, 'brier_score') is not None and metric(left_rows, 'brier_score') is not None else None,
                    'left_log_loss': metric(left_rows, 'log_loss'),
                    'right_log_loss': metric(right_rows, 'log_loss'),
                    'log_loss_delta_right_minus_left': (metric(right_rows, 'log_loss') - metric(left_rows, 'log_loss')) if metric(right_rows, 'log_loss') is not None and metric(left_rows, 'log_loss') is not None else None,
                    'left_probability_dispersion': probability_dispersion(left_rows),
                    'right_probability_dispersion': probability_dispersion(right_rows),
                })
        return jsonify(
            ok=True,
            status='experimental_unvalidated',
            calibration_validated=False,
            candidate_generation_enabled=False,
            paper_trading_enabled=False,
            model_versions=versions,
            comparisons=comparisons,
            limitations=[
                'Read-only shadow comparison based on stored forecasts; it does not execute or activate any model.',
                'Comparisons use only events evaluated by both model versions.',
                'Different versions may cover different periods; shared-match counts are required for interpretation.',
                'No candidates, positions, bankroll changes or betting activation are performed.'
            ]
        )
    except RuntimeError:
        return jsonify(ok=False, error='missing_supabase_config'), 500
    except urllib.error.HTTPError as exc:
        return jsonify(ok=False, error='shadow_comparison_upstream_http_error', upstream_status=exc.code), 502
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, TypeError):
        return jsonify(ok=False, error='shadow_comparison_upstream_unavailable'), 502

"""Read-only validation health and progression dashboard; never activates models or betting."""
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from statistics import mean, pstdev
from flask import Flask, jsonify

app = Flask(__name__)

TARGETS = {
    'distinct_matches': 300,
    'valid_market_matches': 200,
    'valid_closing_matches': 200,
    'brier_score': 0.6667,
    'log_loss': 1.0986,
}


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
    raise ValueError('validation_health_scan_limit_reached')


def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None


def recent_unique_matches(rows, weeks, field='recorded_at'):
    now = datetime.now(timezone.utc)
    start = now - timedelta(weeks=weeks)
    events = set()
    for row in rows:
        when = parse_dt(row.get(field))
        if when and when >= start:
            events.add(row.get('event_id'))
    return len(events)


def eta(remaining, four_week_rate):
    if remaining <= 0:
        return 0.0
    weekly_rate = four_week_rate / 4
    if weekly_rate <= 0:
        return None
    return remaining / weekly_rate


def metric(rows, field):
    values = [float(row[field]) for row in rows if row.get(field) is not None]
    return mean(values) if values else None


def dispersion(rows):
    keys = [
        ('home_probability', 'draw_probability', 'away_probability'),
        ('home_win_probability', 'draw_probability', 'away_win_probability'),
        ('home_win', 'draw', 'away_win'),
    ]
    selected = None
    for candidate in keys:
        if all(any(k in row for row in rows) for k in candidate):
            selected = candidate
            break
    if not selected:
        return {'available': False, 'reason': 'probability_fields_unavailable'}
    home, draw, away = selected
    result = {}
    for label, key in (('home_win', home), ('draw', draw), ('away_win', away)):
        values = [float(row[key]) for row in rows if row.get(key) is not None]
        if not values:
            result[label] = {'available': False}
            continue
        result[label] = {
            'available': True,
            'mean': mean(values),
            'stddev': pstdev(values) if len(values) > 1 else 0.0,
            'min': min(values),
            'max': max(values),
            'range': max(values) - min(values),
        }
    return {'available': True, 'metrics': result}


@app.after_request
def no_store(response):
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.get('/api/validation-health')
def validation_health():
    try:
        samples = read_view('football_quality_samples_with_closing_v1', 'forecast_id.asc')
        distinct_matches = len({row['event_id'] for row in samples})
        market_matched = [row for row in samples if row.get('market_log_loss') is not None and row.get('market_brier_score') is not None]
        closing_keys = [key for key in samples[0].keys() if key.startswith('closing_')] if samples else []
        closing_matched = [row for row in samples if 'closing_log_loss' in closing_keys and row.get('closing_log_loss') is not None and row.get('closing_brier_score') is not None]
        brier = metric(samples, 'brier_score')
        log_loss = metric(samples, 'log_loss')
        rates = {}
        for weeks in (2, 4, 8):
            rates[f'last_{weeks}_weeks'] = {
                'distinct_matches': recent_unique_matches(samples, weeks),
                'market_matched_matches': recent_unique_matches(market_matched, weeks),
                'closing_matched_matches': recent_unique_matches(closing_matched, weeks),
            }
        latest = rates['last_4_weeks']
        progression = {
            'distinct_matches': {
                'current': distinct_matches,
                'target': TARGETS['distinct_matches'],
                'remaining': max(0, TARGETS['distinct_matches'] - distinct_matches),
                'weekly_rate': latest['distinct_matches'],
                'estimated_weeks': eta(max(0, TARGETS['distinct_matches'] - distinct_matches), latest['distinct_matches']),
            },
            'valid_market_matches': {
                'current': len(market_matched),
                'target': TARGETS['valid_market_matches'],
                'remaining': max(0, TARGETS['valid_market_matches'] - len(market_matched)),
                'weekly_rate': latest['market_matched_matches'],
                'estimated_weeks': eta(max(0, TARGETS['valid_market_matches'] - len(market_matched)), latest['market_matched_matches']),
            },
            'valid_closing_matches': {
                'current': len(closing_matched),
                'target': TARGETS['valid_closing_matches'],
                'remaining': max(0, TARGETS['valid_closing_matches'] - len(closing_matched)),
                'weekly_rate': latest['closing_matched_matches'],
                'estimated_weeks': eta(max(0, TARGETS['valid_closing_matches'] - len(closing_matched)), latest['closing_matched_matches']),
            },
            'brier_score': {
                'current': brier,
                'target': TARGETS['brier_score'],
                'passed': brier is not None and brier <= TARGETS['brier_score'],
            },
            'log_loss': {
                'current': log_loss,
                'target': TARGETS['log_loss'],
                'passed': log_loss is not None and log_loss <= TARGETS['log_loss'],
            },
        }
        competitions = []
        for competition in sorted({row.get('competition') for row in samples if row.get('competition')}):
            rows = [row for row in samples if row.get('competition') == competition]
            competitions.append({
                'competition': competition,
                'distinct_matches': len({row['event_id'] for row in rows}),
                'brier_score': metric(rows, 'brier_score'),
                'log_loss': metric(rows, 'log_loss'),
                'probability_dispersion': dispersion(rows),
            })
        return jsonify(
            ok=True,
            status='experimental_unvalidated',
            calibration_validated=False,
            candidate_generation_enabled=False,
            paper_trading_enabled=False,
            targets=TARGETS,
            progression=progression,
            collection_rates=rates,
            competitions=competitions,
            limitations=[
                'Read-only analysis; no candidates, positions, bankroll changes or model activation are performed.',
                'ETA is an estimate based on observed recent collection rates and is not a commitment.',
                'Closing references use the latest complete same-provider 1X2 snapshot captured after the entry reference and before kickoff.',
                'Calibration remains unvalidated until all pre-registered volume, quality and stability criteria are met.'
            ]
        )
    except RuntimeError:
        return jsonify(ok=False, error='missing_supabase_config'), 500
    except urllib.error.HTTPError as exc:
        return jsonify(ok=False, error='validation_health_upstream_http_error', upstream_status=exc.code), 502
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, TypeError, IndexError):
        return jsonify(ok=False, error='validation_health_upstream_unavailable'), 502

"""Read-only deduplicated prediction quality; never activates models or betting."""
import json
import os
import urllib.error
import urllib.request
from statistics import mean
from flask import Flask, jsonify

app = Flask(__name__)
POLICY = 'latest_recorded_pre_kickoff_forecast_v1'

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
    raise ValueError('quality_scan_limit_reached')

def average(rows, field):
    values = [float(r[field]) for r in rows if r.get(field) is not None]
    return mean(values) if values else None

def summarize(rows, scope, group_key):
    result = {'scope': scope, 'group_key': group_key, 'evaluated_forecasts': len(rows), 'distinct_matches': len({r['event_id'] for r in rows}), 'model_versions': len({r['model_version'] for r in rows}), 'log_loss': average(rows, 'log_loss'), 'brier_score': average(rows, 'brier_score'), 'uniform_log_loss': average(rows, 'uniform_log_loss'), 'uniform_brier_score': average(rows, 'uniform_brier_score')}
    for prefix in ('historical', 'market'):
        matched = [r for r in rows if r.get(prefix + '_log_loss') is not None and r.get(prefix + '_brier_score') is not None]
        result[prefix + '_matched_forecasts'] = len(matched)
        result['model_log_loss_on_' + prefix + '_subset'] = average(matched, 'log_loss')
        result['model_brier_on_' + prefix + '_subset'] = average(matched, 'brier_score')
        result[prefix + '_log_loss'] = average(matched, prefix + '_log_loss')
        result[prefix + '_brier_score'] = average(matched, prefix + '_brier_score')
    kickoffs = [r['kickoff_at_snapshot'] for r in rows if r.get('kickoff_at_snapshot')]
    result['first_kickoff'] = min(kickoffs) if kickoffs else None
    result['last_kickoff'] = max(kickoffs) if kickoffs else None
    return result

@app.after_request
def no_store(response):
    response.headers['Cache-Control'] = 'no-store'
    return response

@app.get('/api/prediction-quality')
@app.get('/api/quality')
def quality():
    try:
        samples = read_view('football_quality_samples_deduplicated_v1', 'forecast_id.asc')
        if len(samples) != len({r['event_id'] for r in samples}):
            raise ValueError('duplicate_events_in_deduplicated_source')
        raw = read_view('football_quality_samples_v1', 'forecast_id.asc')
        cohorts = read_view('football_quality_metrics_by_cohort_v1', 'model_cohort.asc,competition.asc')
        summary = [summarize(samples, 'overall', 'all')]
        for scope in ('competition', 'model'):
            groups = {}
            for row in samples:
                key = row['competition'] if scope == 'competition' else row['competition'] + ' / ' + row['model_version']
                groups.setdefault(key, []).append(row)
            summary.extend(summarize(rows, scope, key) for key, rows in sorted(groups.items()))
        for row in cohorts:
            matched = [s for s in samples if s.get('model_cohort') == row['model_cohort'] and s['competition'] == row['competition']]
            row['market_matched_forecasts'] = sum(s.get('market_log_loss') is not None and s.get('market_brier_score') is not None for s in matched)
            row['calibration_validated'] = False
        cohort_summary = []
        for cohort in sorted({r.get('model_cohort') or 'legacy_no_generator' for r in samples}):
            rows = [r for r in samples if (r.get('model_cohort') or 'legacy_no_generator') == cohort]
            cohort_summary.append(dict(summarize(rows, 'cohort', cohort), model_cohort=cohort))
        normalized_samples = [dict(r, selection_policy=POLICY) for r in samples]
        counts = {'evaluated_forecasts': len(samples), 'distinct_matches': len(samples), 'model_versions': len({r['model_version'] for r in samples}), 'excluded_forecasts': len(raw) - len(samples), 'raw_evaluated_forecasts': len(raw)}
        return jsonify(ok=True, status='experimental_unvalidated', calibration_validated=False, counts=counts, summary=summary, cohorts=cohorts, cohort_summary=cohort_summary, samples=normalized_samples, selection_policy=POLICY, probabilities='raw', limitations=[
            'One latest recorded valid pre-kickoff forecast per event, selected from football_quality_samples_v1. Raw history is preserved.',
            'The selection policy is retrospective on historical matches and does not establish prospective validation.',
            'Cohorts are assigned after global event deduplication; they need not cover the same matches and are not a paired model comparison.',
            'Legacy without a generator is a provenance category, not an automatic anomaly label.',
            'Overall summaries mix models and do not measure the current generator alone.',
            'Historical and market comparisons use only samples where both reference scores are present. Missing is not zero.',
            'Market references require a complete same-provider same-time 1X2 snapshot captured and stored before prediction, at most six hours old; these are not necessarily closing odds.',
            'Counts are computed live; paginated reads of separate views are not a single database snapshot.',
            'Calibration remains unvalidated. No candidates, positions, bankroll changes or model activation are performed.'
        ])
    except RuntimeError:
        return jsonify(ok=False, error='missing_supabase_config'), 500
    except urllib.error.HTTPError as exc:
        return jsonify(ok=False, error='quality_upstream_http_error', upstream_status=exc.code), 502
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, TypeError):
        return jsonify(ok=False, error='quality_upstream_unavailable'), 502

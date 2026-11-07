"""Read-only price-pair analytics, separate from forecast evidence."""
import json
import os
import math
import urllib.request
from collections import defaultdict, Counter
from datetime import datetime, timezone
from flask import Flask, jsonify

app = Flask(__name__)

def read_all(table, select):
    base = os.environ.get('SUPABASE_URL', '').rstrip('/')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
    if not base or not key:
        raise RuntimeError('missing_supabase_config')
    rows = []
    offset = 0
    while True:
        req = urllib.request.Request(f'{base}/rest/v1/{table}?select={select}&order=id.asc&offset={offset}&limit=500', headers={'apikey': key, 'Authorization': f'Bearer {key}'})
        with urllib.request.urlopen(req, timeout=30) as response:
            page = json.load(response)
        rows.extend(page)
        if not page:
            return rows
        offset += len(page)

def stamp(value):
    if not value:
        raise ValueError('missing_timestamp')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timezone_required')
    return result

def summarize(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[row['competition']].append(row)
    return [{'competition': league, 'observations': len(items), 'distinct_matches': len({r['event_id'] for r in items}), 'average_clv': sum(r['clv'] for r in items) / len(items), 'positive_clv_rate': sum(r['clv'] > 0 for r in items) / len(items)} for league, items in sorted(groups.items())]

@app.after_request
def no_store(response):
    response.headers['Cache-Control'] = 'no-store'
    return response

@app.get('/api/clv-analytics')
def analytics():
    try:
        history = read_all('historical_match_odds', '*')
        events = read_all('football_events_v4', 'id,external_id')
        forecasts = read_all('football_forecasts_v4', 'id,event_id,eligibility,predicted_at,recorded_at,input_cutoff_at')
        external = {e['id']: e['external_id'] for e in events}
        by_event = defaultdict(list)
        for f in forecasts:
            by_event[external.get(f['event_id'])].append(f)
        groups = defaultdict(lambda: defaultdict(list))
        excluded = Counter()
        for r in history:
            if not r.get('event_id') or not r.get('bookmaker') or r.get('market') not in ('home', 'draw', 'away') or r.get('odds_type') not in ('prediction', 'closing'):
                excluded['invalid_identity_or_type'] += 1
                continue
            groups[(r['event_id'], r['market'], r['bookmaker'])][r['odds_type']].append(r)
        observations, forecast_rows = [], []
        now = datetime.now(timezone.utc)
        for (event, market, bookmaker), kinds in groups.items():
            if not kinds['prediction'] or not kinds['closing']:
                excluded['incomplete_pair'] += 1
                continue
            try:
                opening = min(kinds['prediction'], key=lambda r: stamp(r['captured_at']))
                closing = max(kinds['closing'], key=lambda r: stamp(r['captured_at']))
                start, end = stamp(opening['captured_at']), stamp(closing['captured_at'])
                kickoff = stamp(closing['kickoff_at'])
                before, after = float(opening['odds']), float(closing['odds'])
                if not (math.isfinite(before) and math.isfinite(after) and before > 1 and after > 1):
                    raise ValueError('invalid_odds')
                if stamp(opening['kickoff_at']) != kickoff or not start < end <= kickoff or end > now:
                    raise ValueError('invalid_pair_chronology')
            except (ValueError, TypeError, KeyError):
                excluded['invalid_pair_values_or_chronology'] += 1
                continue
            row = {'event_id': event, 'competition': opening.get('league') or 'unknown', 'home_team': opening.get('home_team'), 'away_team': opening.get('away_team'), 'market': market, 'bookmaker': bookmaker, 'prediction_odds': before, 'closing_odds': after, 'clv': before / after - 1, 'prediction_captured_at': opening['captured_at'], 'closing_captured_at': closing['captured_at'], 'kickoff_at': closing['kickoff_at'], 'closing_verified': False}
            observations.append(row)
            candidates = []
            for f in by_event.get(event, []):
                try:
                    predicted, recorded, cutoff = stamp(f['predicted_at']), stamp(f['recorded_at']), stamp(f['input_cutoff_at'])
                    if f['eligibility'] == 'eligible' and start <= predicted <= recorded < kickoff and cutoff <= predicted and end >= predicted and (predicted - start).total_seconds() <= 21600:
                        candidates.append(f)
                except (ValueError, TypeError, KeyError):
                    pass
            if candidates:
                chosen = min(candidates, key=lambda f: stamp(f['predicted_at']))
                forecast_rows.append(dict(row, forecast_id=chosen['id'], predicted_at=chosen['predicted_at']))
            else:
                excluded['pair_without_compatible_protected_forecast'] += 1
        return jsonify(ok=True, status='analysis_only', summary=summarize(forecast_rows), historical_summary=summarize(observations), rows=forecast_rows, observations=observations, diagnostics=dict(excluded), historical_pairs=len(observations), forecast_pairs=len(forecast_rows), limitations=['Historical price movement is not model CLV.', 'Closing labels are collector labels, not verified final bookmaker closing prices.', 'Bookmakers are matched exactly; Pinnacle is not assumed.', 'Pairs with identical timestamps are excluded.', 'No model approval or betting recommendation.'])
    except RuntimeError as exc:
        return jsonify(ok=False, error=str(exc)), 500
    except Exception:
        app.logger.exception('CLV read failed')
        return jsonify(ok=False, error='clv_source_unavailable'), 502

"""Retain latest observed pre-kickoff prices; never claim verified final closes."""
import json
import os
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify

app = Flask(__name__)
CONFLICT = 'match_date,league,home_team,away_team,market,bookmaker,odds_type'

def api(path, method='GET', payload=None):
    base = os.environ.get('SUPABASE_URL', '').rstrip('/')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
    if not base or not key:
        raise RuntimeError('missing_supabase_config')
    req = urllib.request.Request(base + '/rest/v1/' + path, method=method, headers={'apikey':key,'Authorization':'Bearer '+key,'Content-Type':'application/json','Prefer':'resolution=ignore-duplicates,return=representation'}, data=json.dumps(payload).encode() if payload is not None else None)
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)

def read(table, params):
    rows, offset = [], 0
    while True:
        page = api(table + '?' + urllib.parse.urlencode(dict(params, offset=offset, limit=500)))
        rows.extend(page)
        if not page:
            return rows
        offset += len(page)

def stamp(value):
    result = datetime.fromisoformat(value.replace('Z','+00:00'))
    if result.tzinfo is None:
        raise ValueError('timezone_required')
    return result

@app.after_request
def no_store(response):
    response.headers['Cache-Control'] = 'no-store'
    return response

@app.get('/api/capture-closing-odds')
def capture():
    try:
        now = datetime.now(timezone.utc)
        events = read('football_events_v4', {'select':'id,external_id,source,competition,home_team,away_team,kickoff_at','source':'eq.football_data_org','kickoff_at':'gte.'+(now-timedelta(hours=24)).isoformat(),'order':'id.asc'})
        events = [e for e in events if e.get('kickoff_at') and stamp(e['kickoff_at']) <= now+timedelta(hours=2)]
        inserted = advanced = stale = rejected = 0
        for event in events:
            kickoff = stamp(event['kickoff_at'])
            prices = read('live_odds', {'select':'id,market,provider,odds,captured_at','event_id':'eq.'+event['external_id'],'captured_at':'lte.'+min(now,kickoff).isoformat(),'order':'captured_at.desc,id.asc'})
            latest = {}
            for price in prices:
                try:
                    age = (kickoff-stamp(price['captured_at'])).total_seconds()
                    if price['market'] not in ('home','draw','away') or not price.get('provider') or not 0 <= age <= 21600 or float(price['odds']) <= 1:
                        continue
                    latest.setdefault((price['market'],price['provider']),price)
                except (ValueError,TypeError,KeyError):
                    rejected += 1
            for (market,provider), price in latest.items():
                row = {'event_id':event['external_id'],'match_date':kickoff.date().isoformat(),'league':event['competition'],'home_team':event['home_team'],'away_team':event['away_team'],'market':market,'bookmaker':provider,'odds_type':'closing','odds':price['odds'],'captured_at':price['captured_at'],'kickoff_at':event['kickoff_at'],'source':'live_odds'}
                created = api('historical_match_odds?on_conflict='+CONFLICT,'POST',row)
                inserted += len(created)
                filters = {k:'eq.'+str(row[k]) for k in CONFLICT.split(',')}
                filters['event_id'] = 'eq.'+event['external_id']
                filters['captured_at'] = 'lt.'+price['captured_at']
                changed = api('historical_match_odds?'+urllib.parse.urlencode(filters),'PATCH',{'odds':price['odds'],'captured_at':price['captured_at'],'kickoff_at':event['kickoff_at'],'source':'live_odds'})
                advanced += len(changed)
                stale += int((kickoff-stamp(price['captured_at'])).total_seconds()>900)
        return jsonify(ok=True,events_processed=len(events),closing_rows_inserted=inserted,closing_rows_advanced=advanced,candidates_older_than_15_minutes=stale,rejected_rows=rejected,closing_verified=False,limitations=['Uses recorded live observations only; does not fetch fresh bookmaker prices.','15 minutes is an operational freshness threshold, not proof of a final close.','No retrospective forecasts are generated.'])
    except RuntimeError as exc:
        return jsonify(ok=False,error=str(exc)),500
    except Exception:
        app.logger.exception('Closing capture failed')
        return jsonify(ok=False,error='closing_capture_failed'),502

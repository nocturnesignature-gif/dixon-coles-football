import os, json, hmac, math, uuid, urllib.request, urllib.error
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from flask import Flask, request, jsonify
app=Flask(__name__)
def sb(method,path,payload=None):
    base=os.environ.get('SUPABASE_URL','').rstrip('/')
    key=os.environ.get('SUPABASE_SERVICE_ROLE_KEY','')
    if not base or not key: raise RuntimeError('missing_supabase_config')
    req=urllib.request.Request(base+'/rest/v1/'+path,method=method,data=json.dumps(payload).encode() if payload is not None else None,headers={'apikey':key,'Authorization':'Bearer '+key,'Content-Type':'application/json','Accept':'application/json'})
    with urllib.request.urlopen(req,timeout=20) as r: return json.load(r)
def dt(v):
    try:
        d=datetime.fromisoformat(str(v).replace('Z','+00:00'))
        return d if d.tzinfo else None
    except (ValueError,TypeError): return None
def money(v):
    if isinstance(v,bool): raise ValueError('invalid_amount')
    d=Decimal(str(v))
    if not d.is_finite() or d<0 or d>1000000000 or d!=d.quantize(Decimal('0.01')): raise ValueError('invalid_amount')
    return str(d)
@app.after_request
def headers(r):
    r.headers['Cache-Control']='no-store'
    return r
@app.get('/api/paper-bot')
def status():
    try:
        portfolios=sb('GET','virtual_portfolios?order=name.asc')
        positions=sb('GET','virtual_portfolio_positions?order=placed_at.desc&limit=200')
        legs=sb('GET','virtual_portfolio_legs?order=created_at.desc&limit=2000')
        candidates=sb('GET','strategy_candidates?limit=2000')
        byid={c['id']:c for c in candidates}
        enriched=[]
        for l in legs:
            c=byid.get(l['candidate_id'],{})
            enriched.append({**l,**{k:c.get(k) for k in ('home_team','away_team','competition','kickoff_at','event_id')}})
        now=datetime.now(timezone.utc)
        for p in positions:
            ls=[l for l in enriched if l['position_id']==p['id']]
            times=[dt(l.get('kickoff_at')) for l in ls]
            placed=dt(p.get('placed_at'))
            invalid=bool(placed and any(t and t<=placed for t in times))
            p['temporal_invalid']=invalid
            p['display_group']='history' if p['status']!='open' else 'invalid' if invalid else 'unknown' if not times or any(t is None for t in times) else 'upcoming' if all(t>now for t in times) else 'awaiting_result'
        policies={'singles':{'max_stake_fraction':0.005,'max_exposure_fraction':0.02},'combo_2':{'max_stake_fraction':0.0025,'max_exposure_fraction':0.01}}
        for p in portfolios:
            policy=policies.get(p['strategy_type'],{'max_stake_fraction':0,'max_exposure_fraction':0})
            available=max(0,float(p['current_bankroll']))
            exposure=max(0,float(p['open_exposure']))
            capital=available+exposure
            cap=min(available,capital*policy['max_stake_fraction'],max(0,capital*policy['max_exposure_fraction']-exposure))
            p['risk_policy']={**policy,'maximum_next_virtual_stake':math.floor(max(0,cap)*100)/100,'execution_enabled':False,'confidence':'experimental_uncalibrated'}
        return jsonify(ok=True,portfolios=portfolios,positions=positions,legs=enriched,execution_enabled=False,admin_edit_configured=bool(os.environ.get('PAPER_BOT_ADMIN_TOKEN')),disclaimer='Simulation virtuelle. Calibration prospective non validée. Aucune recommandation de pari.')
    except Exception:
        app.logger.exception('paper status failed')
        return jsonify(ok=False,error='paper_bot_status_failed'),502
@app.post('/api/paper-bot')
def adjust():
    secret=os.environ.get('PAPER_BOT_ADMIN_TOKEN','')
    if not secret: return jsonify(ok=False,error='admin_edit_not_configured'),503
    provided=request.headers.get('Authorization','')
    if not hmac.compare_digest(provided.encode(),('Bearer '+secret).encode()): return jsonify(ok=False,error='unauthorized'),401
    if request.headers.get('Origin') and request.headers['Origin']!=request.host_url.rstrip('/'):
        return jsonify(ok=False,error='origin_not_allowed'),403
    if not request.is_json: return jsonify(ok=False,error='json_required'),415
    try:
        b=request.get_json()
        if not isinstance(b,dict): raise ValueError('invalid_body')
        reason=b.get('reason','')
        if not isinstance(reason,str) or not 1<=len(reason.strip())<=500: raise ValueError('reason_required')
        payload={'p_request_id':str(uuid.UUID(b['request_id'])),'p_portfolio_id':str(uuid.UUID(b['portfolio_id'])),'p_expected_available':money(b['expected_available']),'p_new_available':money(b['new_available']),'p_reason':reason.strip()}
        return jsonify(sb('POST','rpc/paper_bot_set_available_v1',payload))
    except (ValueError,TypeError,KeyError,InvalidOperation): return jsonify(ok=False,error='invalid_adjustment'),400
    except urllib.error.HTTPError as e:
        body=e.read().decode(errors='replace')
        if 'bankroll_changed_refresh_required' in body or 'request_id_conflict' in body: return jsonify(ok=False,error='conflict_refresh_required'),409
        app.logger.error('bankroll RPC HTTP %s',e.code)
        return jsonify(ok=False,error='bankroll_update_failed'),502
    except Exception:
        app.logger.exception('bankroll update failed')
        return jsonify(ok=False,error='bankroll_update_failed'),502
@app.post('/api/paper-bot/simulate')
def simulate():
    return jsonify(ok=False,error='execution_disabled_pending_transactional_engine',virtual_only=True),503

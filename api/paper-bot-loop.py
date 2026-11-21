import os, json, hmac, uuid, hashlib, urllib.request, urllib.error
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from flask import Flask, request, jsonify

app = Flask(__name__)
VERSION = 'paper-loop-risk-audit-v3'

def sb(method, path, payload=None):
    base = os.environ.get('SUPABASE_URL', '').rstrip('/')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
    if not base or not key:
        raise RuntimeError('missing_supabase_config')
    req = urllib.request.Request(base + '/rest/v1/' + path, method=method, data=json.dumps(payload).encode() if payload is not None else None, headers={'apikey': key, 'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', 'Accept': 'application/json', 'Prefer': 'return=representation'})
    with urllib.request.urlopen(req, timeout=20) as response:
        raw = response.read().decode()
        return json.loads(raw) if raw else []

def authorized():
    header = request.headers.get('Authorization', '')
    if not header.startswith('Bearer ') or len(header) > 512:
        return False
    token = header[7:]
    cron = os.environ.get('CRON_SECRET', '')
    if request.method == 'GET' and cron and hmac.compare_digest(hashlib.sha256(token.encode()).digest(), hashlib.sha256(cron.encode()).digest()):
        return True
    secret = os.environ.get('PAPER_BOT_ADMIN_TOKEN', '')
    if secret:
        expected = hashlib.sha256(secret.encode()).hexdigest()
    else:
        rows = sb('GET', 'paper_bot_admin_credentials?id=eq.1&enabled=eq.true&select=token_sha256&limit=1')
        expected = rows[0].get('token_sha256', '') if rows else ''
    return bool(expected) and hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), expected)

def number(value):
    if value is None or isinstance(value, bool):
        raise ValueError('invalid_number')
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('invalid_number')
    return result

def timestamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result if result.tzinfo else None
    except (ValueError, TypeError):
        return None

def assess(candidate, portfolio, now):
    kickoff = timestamp(candidate.get('kickoff_at'))
    captured = timestamp(candidate.get('opening_captured_at'))
    if not kickoff:
        return 'kickoff_missing', None
    if kickoff <= now:
        return 'match_already_started', None
    if not captured:
        return 'odds_timestamp_missing', None
    age = (now - captured).total_seconds()
    if age < 0 or age > 43200:
        return 'odds_stale_or_future', None
    try:
        odds = number(candidate.get('opening_odds'))
        probability = number(candidate.get('model_probability'))
        implied = number(candidate.get('implied_probability'))
        if not (1 < odds < 1000000 and 0 < probability < 1 and 0 < implied < 1):
            return 'invalid_probability_or_odds', None
        edge = probability - implied
        if edge < Decimal('0.03') or probability * odds <= 1:
            return 'insufficient_edge', None
        available = number(portfolio['current_bankroll'])
        exposure = number(portfolio['open_exposure'])
        fraction = number(portfolio['max_stake_fraction'])
        kelly = number(portfolio['kelly_fraction'])
        if available < 0 or exposure < 0 or not (0 < fraction <= Decimal('0.05') and 0 < kelly <= 1):
            return 'invalid_portfolio', None
        capital = available + exposure
        cap = capital * Decimal('0.02')
        room = max(Decimal(0), cap - exposure)
        raw = available * (probability * odds - 1) / (odds - 1) * kelly
        stake = min(raw, available, available * fraction, capital * Decimal('0.005'), room).quantize(Decimal('0.01'), rounding=ROUND_DOWN)
        if stake <= 0:
            return 'no_stake_or_exposure_room', None
        return None, {'stake': str(stake), 'odds': str(odds), 'probability': str(probability), 'implied': str(implied), 'edge': str(edge), 'exposure_cap': str(cap)}
    except (ValueError, InvalidOperation, KeyError, TypeError):
        return 'invalid_candidate_or_portfolio', None

@app.after_request
def no_store(response):
    response.headers['Cache-Control'] = 'no-store'
    return response

@app.route('/api/paper-bot-loop', methods=['GET', 'POST'])
def loop():
    run_id = str(uuid.uuid4())
    state = {'created': 0, 'skipped': 0, 'rejected': 0, 'already_processed': 0, 'results': [], 'run_id': run_id, 'version': VERSION, 'virtual_only': True, 'writes_scope': 'new_positions'}
    def finish(ok=True, status='executed', code=200):
        return jsonify(ok=ok, status=status, writes=state['created'], disclaimer='Simulation virtuelle uniquement. Calibration prospective non validée. Aucun pari réel.', **state), code
    def audit_skip(portfolio, candidate, reason):
        sb('POST', 'paper_bot_activity_log', {'portfolio_id': portfolio['id'], 'run_id': run_id, 'action': 'rejection', 'candidate_id': candidate['id'], 'status': 'skipped', 'reason': reason, 'details': {'stage': 'python_filter', 'virtual_only': True}})
        state['skipped'] += 1
        state['results'].append({'portfolio': portfolio['name'], 'candidate_id': candidate['id'], 'status': 'skipped', 'reason': reason})
    try:
        if not authorized():
            return finish(False, 'unauthorized', 401)
        if request.headers.get('Origin') and request.headers['Origin'] != request.host_url.rstrip('/'):
            return finish(False, 'origin_not_allowed', 403)
        portfolios = sb('GET', 'virtual_portfolios?strategy_type=eq.singles&is_active=eq.true&order=name.asc')
        if not portfolios:
            return finish(status='no_active_portfolio')
        candidates = sb('GET', 'strategy_candidates?status=eq.eligible&order=created_at.desc,id.asc&limit=300&select=id,competition,market,selection,model_probability,implied_probability,opening_odds,opening_captured_at,kickoff_at,event_id')
        state['candidates_loaded'] = len(candidates)
        state['candidate_scan_may_be_truncated'] = len(candidates) == 300
        for portfolio in portfolios:
            for candidate in candidates:
                if state['created'] >= 3:
                    return finish()
                now = datetime.now(timezone.utc)
                reason, sizing = assess(candidate, portfolio, now)
                if reason:
                    audit_skip(portfolio, candidate, reason)
                    continue
                request_id = str(uuid.uuid4())
                context = {'portfolio': portfolio['name'], 'candidate_id': candidate['id'], 'request_id': request_id, 'stake': sizing['stake']}
                leg = {'candidate_id': candidate['id'], 'selection': candidate.get('selection'), 'market': candidate.get('market'), 'model_probability': sizing['probability'], 'implied_probability_net': sizing['implied'], 'edge': sizing['edge'], 'opening_odds': sizing['odds'], 'odds_taken': sizing['odds'], 'run_id': run_id}
                state['pending_rpc'] = context
                outcome = sb('POST', 'rpc/create_paper_position_atomic', {'p_request_id': request_id, 'p_portfolio_id': portfolio['id'], 'p_strategy_type': 'singles', 'p_stake_amount': sizing['stake'], 'p_combined_odds': sizing['odds'], 'p_combined_probability': sizing['probability'], 'p_max_open_exposure': sizing['exposure_cap'], 'p_legs': [leg]})
                status = outcome.get('status') if isinstance(outcome, dict) else None
                if status not in ('created', 'rejected', 'already_processed'):
                    raise RuntimeError('unexpected_rpc_response')
                state.pop('pending_rpc', None)
                if status == 'rejected':
                    state['rejected'] += 1
                    state['skipped'] += 1
                    state['results'].append({**context, 'status': status, 'reason': outcome.get('reason') or 'unspecified_rejection'})
                    continue
                if status == 'already_processed':
                    state['already_processed'] += 1
                    state['skipped'] += 1
                    state['results'].append({**context, 'status': status, 'position_id': outcome.get('position_id')})
                    break
                state['created'] += 1
                state['results'].append({**context, 'status': 'created', 'position_id': outcome.get('position_id')})
                available = number(outcome.get('remaining_bankroll'))
                exposure = number(outcome.get('open_exposure'))
                if not outcome.get('position_id') or available < 0 or exposure < 0:
                    raise RuntimeError('invalid_created_rpc_response')
                portfolio['current_bankroll'] = str(available)
                portfolio['open_exposure'] = str(exposure)
        return finish()
    except Exception:
        app.logger.exception('paper bot execution stopped')
        if 'pending_rpc' in state:
            state['results'].append({**state.pop('pending_rpc'), 'status': 'unconfirmed', 'reason': 'rpc_outcome_requires_reconciliation'})
        return finish(False, 'paper_bot_loop_failed', 502)

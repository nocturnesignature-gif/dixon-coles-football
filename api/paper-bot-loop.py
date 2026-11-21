import os, json, hmac, math, uuid, hashlib, urllib.request, urllib.error
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from flask import Flask, request, jsonify

app = Flask(__name__)

def sb(method, path, payload=None):
    base = os.environ.get('SUPABASE_URL', '').rstrip('/')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
    if not base or not key:
        raise RuntimeError('missing_supabase_config')
    req = urllib.request.Request(base + '/rest/v1/' + path, method=method, data=json.dumps(payload).encode() if payload is not None else None, headers={'apikey': key, 'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', 'Accept': 'application/json', 'Prefer': 'return=representation'})
    with urllib.request.urlopen(req, timeout=25) as response:
        raw = response.read().decode('utf-8')
        return json.loads(raw) if raw else []

def admin_digest():
    secret = os.environ.get('PAPER_BOT_ADMIN_TOKEN', '')
    if secret:
        return hashlib.sha256(secret.encode()).hexdigest()
    rows = sb('GET', 'paper_bot_admin_credentials?id=eq.1&enabled=eq.true&select=token_sha256&limit=1')
    digest = rows[0].get('token_sha256', '') if rows else ''
    return digest if len(digest) == 64 and all(c in '0123456789abcdef' for c in digest) else ''

def authorized():
    provided = request.headers.get('Authorization', '')
    if not provided.startswith('Bearer ') or len(provided) > 512:
        return False
    expected = admin_digest()
    if not expected:
        return False
    supplied = hashlib.sha256(provided[7:].encode()).hexdigest()
    return hmac.compare_digest(supplied, expected)

def parse_dt(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None

def decimal_or_none(value):
    if value in (None, ''):
        return None
    try:
        value = Decimal(str(value))
        return float(value) if value.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None

def kelly_stake(probability, odds, bankroll, kelly_fraction, max_stake_fraction):
    if not probability or not odds or odds <= 1:
        return 0.0
    edge = probability * odds - 1
    if edge <= 0:
        return 0.0
    kelly = (probability * odds - 1) / (odds - 1)
    fraction = min(kelly * kelly_fraction, max_stake_fraction)
    return max(0.0, math.floor(bankroll * fraction * 100) / 100)

@app.after_request
def headers(response):
    response.headers['Cache-Control'] = 'no-store'
    return response

@app.route('/api/paper-bot-loop', methods=['GET', 'POST'])
def loop():
    if not authorized():
        return jsonify(ok=False, error='unauthorized'), 401
    if request.headers.get('Origin') and request.headers['Origin'] != request.host_url.rstrip('/'):
        return jsonify(ok=False, error='origin_not_allowed'), 403
    created = 0
    skipped = 0
    rejected = 0
    already_processed = 0
    results = []
    try:
        now = datetime.now(timezone.utc)
        max_positions = 3
        min_edge = 0.03
        max_odds_age_hours = 12
        portfolios = sb('GET', 'virtual_portfolios?strategy_type=eq.singles&is_active=eq.true&order=name.asc')
        if not portfolios:
            return jsonify(ok=True, status='no_active_portfolio', virtual_only=True, created=0, skipped=0, rejected=0, already_processed=0, writes=0, writes_scope='new_positions', results=[])
        candidates = sb('GET', 'strategy_candidates?status=eq.eligible&order=created_at.asc&limit=300&select=id,competition,home_team,away_team,market,selection,model_probability,implied_probability,edge,opening_odds,opening_captured_at,kickoff_at,event_id')
        open_positions = sb('GET', 'virtual_portfolio_positions?status=eq.open&select=id')
        position_ids = [row['id'] for row in open_positions]
        used_candidates = set()
        if position_ids:
            legs = sb('GET', 'virtual_portfolio_legs?position_id=in.(' + ','.join(position_ids) + ')&select=candidate_id')
            used_candidates = {row['candidate_id'] for row in legs}
        for portfolio in portfolios:
            if created >= max_positions:
                break
            bankroll = decimal_or_none(portfolio.get('current_bankroll')) or 0.0
            exposure = decimal_or_none(portfolio.get('open_exposure')) or 0.0
            kelly_fraction = decimal_or_none(portfolio.get('kelly_fraction')) or 0.25
            max_stake_fraction = decimal_or_none(portfolio.get('max_stake_fraction')) or 0.01
            capital = bankroll + exposure
            max_open_exposure = capital * 0.02
            exposure_room = max(0.0, max_open_exposure - exposure)
            for candidate in candidates:
                if created >= max_positions:
                    break
                if candidate['id'] in used_candidates:
                    skipped += 1
                    continue
                kickoff = parse_dt(candidate.get('kickoff_at'))
                captured = parse_dt(candidate.get('opening_captured_at'))
                odds = decimal_or_none(candidate.get('opening_odds'))
                probability = decimal_or_none(candidate.get('model_probability'))
                edge = decimal_or_none(candidate.get('edge')) or 0.0
                if not kickoff or kickoff <= now:
                    skipped += 1
                    continue
                if not captured or (now - captured).total_seconds() > max_odds_age_hours * 3600:
                    skipped += 1
                    continue
                if not odds or odds <= 1 or not probability or probability <= 0 or probability >= 1:
                    skipped += 1
                    continue
                if edge < min_edge:
                    skipped += 1
                    continue
                stake = kelly_stake(probability, odds, bankroll, kelly_fraction, max_stake_fraction)
                stake = min(stake, bankroll, exposure_room)
                stake = math.floor(stake * 100) / 100
                if stake <= 0:
                    skipped += 1
                    continue
                request_id = str(uuid.uuid4())
                leg = {'candidate_id': candidate['id'], 'selection': candidate.get('selection') or candidate.get('market'), 'model_probability': probability, 'implied_probability_net': decimal_or_none(candidate.get('implied_probability')), 'edge': edge, 'opening_odds': odds, 'odds_taken': odds, 'market': candidate.get('market'), 'run_id': 'paper-bot-' + now.strftime('%Y%m%d%H%M%S')}
                context = {'portfolio': portfolio['name'], 'candidate_id': candidate['id'], 'request_id': request_id, 'stake': stake}
                try:
                    outcome = sb('POST', 'rpc/create_paper_position_atomic', {'p_request_id': request_id, 'p_portfolio_id': portfolio['id'], 'p_strategy_type': 'singles', 'p_stake_amount': stake, 'p_combined_odds': odds, 'p_combined_probability': probability, 'p_max_open_exposure': max_open_exposure, 'p_legs': [leg]})
                    rpc_status = outcome.get('status') if isinstance(outcome, dict) else None
                    if rpc_status == 'rejected':
                        rejected += 1
                        skipped += 1
                        results.append({**context, 'status': 'rejected', 'reason': outcome.get('reason') or 'unspecified_rejection'})
                        continue
                    if rpc_status == 'already_processed':
                        already_processed += 1
                        skipped += 1
                        used_candidates.add(candidate['id'])
                        results.append({**context, 'status': 'already_processed', 'position_id': outcome.get('position_id')})
                        break
                    if rpc_status != 'created':
                        results.append({**context, 'status': 'error', 'reason': 'unexpected_rpc_response'})
                        raise RuntimeError('unexpected_rpc_response')
                    created += 1
                    used_candidates.add(candidate['id'])
                    results.append({**context, 'status': 'created', 'position_id': outcome.get('position_id')})
                    next_bankroll = decimal_or_none(outcome.get('remaining_bankroll'))
                    next_exposure = decimal_or_none(outcome.get('open_exposure'))
                    if not outcome.get('position_id') or next_bankroll is None or next_exposure is None or not math.isfinite(next_bankroll) or not math.isfinite(next_exposure) or next_bankroll < 0 or next_exposure < 0:
                        raise RuntimeError('invalid_created_rpc_response')
                    bankroll = next_bankroll
                    exposure = next_exposure
                    exposure_room = max(0.0, max_open_exposure - exposure)
                except urllib.error.HTTPError as error:
                    skipped += 1
                    results.append({**context, 'status': 'error', 'http_status': error.code, 'reason': 'rpc_http_error_outcome_unconfirmed'})
                    raise
        return jsonify(ok=True, status='executed', virtual_only=True, created=created, skipped=skipped, rejected=rejected, already_processed=already_processed, writes=created, writes_scope='new_positions', results=results, disclaimer='Simulation virtuelle uniquement. Calibration prospective non validée. Aucun pari réel.')
    except urllib.error.HTTPError as error:
        app.logger.error('paper bot loop HTTP %s', error.code)
        return jsonify(ok=False, error='paper_bot_loop_failed', virtual_only=True, created=created, skipped=skipped, rejected=rejected, already_processed=already_processed, writes=created, writes_scope='new_positions', results=results), 502
    except Exception:
        app.logger.exception('paper bot loop failed')
        return jsonify(ok=False, error='paper_bot_loop_failed', virtual_only=True, created=created, skipped=skipped, rejected=rejected, already_processed=already_processed, writes=created, writes_scope='new_positions', results=results), 502

if __name__ == '__main__':
    print(json.dumps({'ok': True, 'status': 'ready', 'virtual_only': True}))

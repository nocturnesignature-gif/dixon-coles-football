def validate_settlement(outcome):
    keys = ('settled', 'pending_or_skipped', 'rejected')
    if not isinstance(outcome, dict) or outcome.get('virtual_only') is not True:
        raise ValueError('invalid_settlement_response')
    if any(type(outcome.get(k)) is not int or outcome[k] < 0 for k in keys):
        raise ValueError('invalid_settlement_counts')
    rows = outcome.get('results')
    if not isinstance(rows, list) or sum(outcome[k] for k in keys) != len(rows) or len(rows) > 50:
        raise ValueError('invalid_settlement_results')
    counts = dict.fromkeys(keys, 0)
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('invalid_settlement_row')
        status = row.get('status')
        if status == 'settled':
            counts['settled'] += 1
        elif status == 'rejected':
            counts['rejected'] += 1
        elif status in ('pending', 'skipped', 'already_settled'):
            counts['pending_or_skipped'] += 1
        else:
            raise ValueError('unknown_settlement_status')
    if any(counts[k] != outcome[k] for k in keys):
        raise ValueError('inconsistent_settlement_counts')
    return outcome


def install_settlement(app, sb, authorized):
    from flask import g, jsonify, request
    if app.extensions.get('paper_settlement_installed'):
        return
    app.extensions['paper_settlement_installed'] = True

    @app.before_request
    def settle_before_execution():
        if request.path.rstrip('/') != '/api/paper-bot-loop' or request.method not in ('GET', 'POST'):
            return None
        try:
            if not authorized():
                return jsonify(ok=False, error='unauthorized', virtual_only=True), 401
            if request.headers.get('Origin') and request.headers['Origin'] != request.host_url.rstrip('/'):
                return jsonify(ok=False, error='origin_not_allowed', virtual_only=True), 403
            g.paper_settlement = {'status': 'unconfirmed'}
            outcome = sb('POST', 'rpc/settle_open_paper_positions_v1', {'p_limit': 50})
            validate_settlement(outcome)
            g.paper_settlement = {'status': 'completed', **outcome}
            if outcome['rejected']:
                return jsonify(ok=False, error='settlement_attention_required', created=0, virtual_only=True), 503
        except Exception:
            app.logger.exception('paper settlement stopped before new positions')
            return jsonify(ok=False, error='settlement_failed', created=0, virtual_only=True), 502
        return None

    @app.after_request
    def include_settlement(response):
        summary = getattr(g, 'paper_settlement', None)
        if summary is not None and response.is_json:
            body = response.get_json(silent=True)
            if isinstance(body, dict):
                body['settlement'] = summary
                body['settlement_integration_version'] = 'settlement-before-execution-v1'
                response.set_data(app.json.dumps(body))
        response.headers['Cache-Control'] = 'no-store'
        return response

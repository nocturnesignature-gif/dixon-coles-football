import json
from flask import Flask, jsonify

app = Flask(__name__)


@app.route('/api/paper-bot-loop', methods=['GET', 'POST'])
def loop():
    return jsonify(
        ok=False,
        status='suspended',
        error='execution_disabled_pending_transactional_engine',
        reason=(
            'Creation and settlement must use virtual_portfolios, '
            'virtual_portfolio_positions and virtual_portfolio_legs atomically.'
        ),
        writes=0,
    ), 503


if __name__ == '__main__':
    print(json.dumps({
        'ok': False,
        'status': 'suspended',
        'writes': 0,
        'error': 'execution_disabled_pending_transactional_engine',
    }))

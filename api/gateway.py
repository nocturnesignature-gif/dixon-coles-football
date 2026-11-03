"""Single WSGI deployment entrypoint; existing API modules are imported, not built separately."""
import importlib.util
import sys
from functools import lru_cache
from pathlib import Path
from flask import Flask, Response, jsonify, request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
app = Flask(__name__)
MODULES = {
    '/api/forecasts': 'forecasts',
    '/api/quality': 'quality',
    '/api/prediction-quality': 'quality',
    '/api/market-value': 'market-value',
    '/api/combo-recommendations': 'combo-recommendations',
    '/api/performance': 'performance',
    '/api/clv-analytics': 'clv-analytics',
    '/api/market-movement': 'market-movement',
    '/api/monte-carlo': 'monte-carlo',
    '/api/strategies': 'strategies',
    '/api/strategy-lab': 'strategy-lab',
    '/api/data-maturity': 'data-maturity',
    '/api/sync-odds': 'sync-odds',
    '/api/capture-closing-odds': 'capture-closing-odds',
}

@lru_cache(maxsize=None)
def load_app(name):
    module_name = 'football_api_' + name.replace('-', '_')
    spec = importlib.util.spec_from_file_location(module_name, ROOT / 'api' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module.app

@app.after_request
def no_store(response):
    response.headers['Cache-Control'] = 'no-store'
    return response

@app.before_request
def dispatch():
    path = request.path.rstrip('/')
    if path == '/api/check-alerts':
        return jsonify(ok=True, status='alerts_disabled', alerts=[],
            reason='prospective_calibration_not_validated',
            guardrails={'bets':'disabled','accumulators':'disabled',
                        'notifications':'disabled','recommendations':'disabled'})
    if path == '/api/bets' and request.method != 'GET':
        return jsonify(ok=False, error='bets_disabled_pending_validation'), 403
    if path.startswith('/api/bets/') and request.method != 'GET':
        return jsonify(ok=False, error='bets_disabled_pending_validation'), 403
    if path == '/api/health':
        return jsonify(ok=True, entrypoint='single_python_gateway', alerts='disabled')
    if not path.startswith('/api/'):
        return jsonify(ok=False, error='not_found'), 404
    name = MODULES.get(path, 'index')
    environ = request.environ.copy()
    if path == '/api/prediction-quality':
        environ['PATH_INFO'] = '/api/quality'
    return Response.from_app(load_app(name), environ)

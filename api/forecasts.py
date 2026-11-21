from api.index import forecasts as index_forecasts
from flask import Flask

app = Flask(__name__)


@app.get("/api/forecasts")
def forecasts():
    return index_forecasts()

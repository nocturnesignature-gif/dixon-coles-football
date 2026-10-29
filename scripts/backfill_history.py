"""Generate walk-forward historical predictions and upload them to Supabase."""

import json
import math
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.dixon_coles import DixonColesModel

LEAGUE_FILES = [
    "data/raw/premier_league.csv",
    "data/raw/la_liga.csv",
    "data/raw/serie_a.csv",
    "data/raw/bundesliga.csv",
    "data/raw/ligue_1.csv",
]
REFIT_EVERY = 500
TRAINING_WINDOW = 800
MIN_TRAINING_MATCHES = 200
MODEL_VERSION = "dixon_coles_walkforward_v1"
MIN_PROBABILITY = 0.000001


def load_matches():
    matches = []
    for path in LEAGUE_FILES:
        if not Path(path).exists():
            print(f"Skipping missing file: {path}", flush=True)
            continue
        df = pd.read_csv(path)
        df = df.dropna(subset=["HomeTeam", "AwayTeam", "FTHG", "FTAG", "Date"])
        df["MatchDate"] = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce")
        df = df.dropna(subset=["MatchDate"])
        for row in df.itertuples(index=False):
            matches.append({
                "league": Path(path).stem,
                "home": str(row.HomeTeam).strip(),
                "away": str(row.AwayTeam).strip(),
                "home_goals": int(row.FTHG),
                "away_goals": int(row.FTAG),
                "date": row.MatchDate.date(),
            })
    matches.sort(key=lambda m: m["date"])
    print(f"Loaded {len(matches)} valid matches", flush=True)
    return matches


def actual_outcome(match):
    if match["home_goals"] > match["away_goals"]:
        return "home"
    if match["home_goals"] == match["away_goals"]:
        return "draw"
    return "away"


def valid_probabilities(probabilities):
    values = [max(min(float(p), 1.0), MIN_PROBABILITY) for p in probabilities]
    total = sum(values)
    return [value / total for value in values]


def metrics_for(probabilities, outcome):
    actual = [1.0 if outcome == "home" else 0.0, 1.0 if outcome == "draw" else 0.0, 1.0 if outcome == "away" else 0.0]
    clipped = np.clip(probabilities, 1e-12, 1)
    log_loss = -sum(a * math.log(p) for a, p in zip(actual, clipped))
    brier = sum((p - a) ** 2 for p, a in zip(probabilities, actual))
    return log_loss, brier


def supabase_upsert(rows):
    base_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base_url or not service_key:
        raise RuntimeError("missing_supabase_config")
    url = f"{base_url}/rest/v1/historical_forecasts"
    request = urllib.request.Request(
        url,
        method="POST",
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        data=json.dumps(rows).encode(),
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:1000]
        print(f"Supabase upsert failed: HTTP {exc.code}", flush=True)
        print(f"URL: {url}", flush=True)
        print(f"Rows in batch: {len(rows)}", flush=True)
        print(f"Response: {body}", flush=True)
        raise


def main():
    matches = load_matches()
    if len(matches) <= MIN_TRAINING_MATCHES:
        raise ValueError(f"Need more than {MIN_TRAINING_MATCHES} matches; loaded {len(matches)}")

    model_fit = None
    last_fit_size = -1
    predictions = []
    batch = []

    for index, match in enumerate(matches):
        if index < MIN_TRAINING_MATCHES:
            continue
        if last_fit_size < 0 or index - MIN_TRAINING_MATCHES >= last_fit_size + REFIT_EVERY:
            training = matches[max(0, index - TRAINING_WINDOW):index]
            model_fit = DixonColesModel(half_life_days=365)
            model_fit.fit([m.copy() for m in training])
            last_fit_size = index
            print(f"Refit {index}: {len(training)} training matches", flush=True)

        try:
            forecast = model_fit.predict(match["home"], match["away"])
        except KeyError:
            continue

        probabilities = forecast["probabilities"]
        outcome = actual_outcome(match)
        picks = {
            "home": probabilities["home_win"],
            "draw": probabilities["draw"],
            "away": probabilities["away_win"],
        }
        home_probability, draw_probability, away_probability = valid_probabilities([
            picks["home"],
            picks["draw"],
            picks["away"],
        ])
        probability_list = [home_probability, draw_probability, away_probability]
        log_loss, brier = metrics_for(probability_list, outcome)

        row = {
            "match_date": match["date"].isoformat(),
            "league": match["league"],
            "home_team": match["home"],
            "away_team": match["away"],
            "home_goals": match["home_goals"],
            "away_goals": match["away_goals"],
            "actual_outcome": outcome,
            "home_probability": home_probability,
            "draw_probability": draw_probability,
            "away_probability": away_probability,
            "log_loss": log_loss,
            "brier_score": brier,
            "model_version": MODEL_VERSION,
        }
        predictions.append(row)
        batch.append(row)

        if len(batch) >= 500:
            supabase_upsert(batch)
            print(f"Uploaded {len(predictions)} predictions", flush=True)
            batch = []

        if index % 500 == 0:
            print(f"Processed {index}/{len(matches)} matches", flush=True)

    if batch:
        supabase_upsert(batch)

    output = REPO_ROOT / "reports"
    output.mkdir(exist_ok=True)
    pd.DataFrame(predictions).to_csv(output / "historical_backfill_predictions.csv", index=False)
    print(f"Completed: {len(predictions)} historical predictions uploaded", flush=True)


if __name__ == "__main__":
    main()

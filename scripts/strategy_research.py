"""Historical strategy research using walk-forward model predictions.

This script evaluates prediction-based strategies without inventing historical
odds. It measures hit rate, calibration, and risk proxies. Monetary ROI and
CLV should be assessed separately with real placed odds and closing odds.
"""

import json
import sys
from collections import defaultdict
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
REFIT_EVERY = 250
TRAINING_WINDOW = 2000
MIN_TRAINING_MATCHES = 200


def load_matches():
    matches = []
    for path in LEAGUE_FILES:
        if not Path(path).exists():
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
    return matches


def outcome(match):
    if match["home_goals"] > match["away_goals"]:
        return "home"
    if match["home_goals"] == match["away_goals"]:
        return "draw"
    return "away"


def strategy_metrics(rows):
    if not rows:
        return {"selections": 0, "hit_rate": None, "average_probability": None, "brier_score": None}
    hits = np.array([r["correct"] for r in rows], dtype=float)
    probs = np.array([r["probability"] for r in rows], dtype=float)
    outcomes = np.array([r["actual"] == r["picked"] for r in rows], dtype=float)
    return {
        "selections": len(rows),
        "hit_rate": float(hits.mean()),
        "average_probability": float(probs.mean()),
        "brier_score": float(np.mean((probs - outcomes) ** 2)),
    }


def main():
    matches = load_matches()
    if len(matches) <= MIN_TRAINING_MATCHES:
        raise ValueError(f"Need more than {MIN_TRAINING_MATCHES} matches")

    model = DixonColesModel(half_life_days=365)
    last_fit = -1
    predictions = []

    for index, match in enumerate(matches):
        if index < MIN_TRAINING_MATCHES:
            continue
        if last_fit < 0 or index - MIN_TRAINING_MATCHES >= last_fit + REFIT_EVERY:
            training = matches[max(0, index - TRAINING_WINDOW):index]
            model.fit([m.copy() for m in training])
            last_fit = index
            print(f"Refit at {index} with {len(training)} matches")

        try:
            forecast = model.predict(match["home"], match["away"])
        except KeyError:
            continue

        probs = forecast["probabilities"]
        actual = outcome(match)
        picks = {
            "home": probs["home_win"],
            "draw": probs["draw"],
            "away": probs["away_win"],
        }
        top = max(picks, key=picks.get)
        predictions.append({
            "league": match["league"],
            "actual": actual,
            "picked": top,
            "probability": picks[top],
            "correct": int(top == actual),
            "home_probability": picks["home"],
            "draw_probability": picks["draw"],
            "away_probability": picks["away"],
        })

    strategies = {
        "favourite": [r for r in predictions if r["picked"] in ("home", "away")],
        "draw": [r for r in predictions if r["picked"] == "draw"],
        "high_probability": [r for r in predictions if r["probability"] >= 0.65],
        "very_high_probability": [r for r in predictions if r["probability"] >= 0.70],
        "home": [r for r in predictions if r["picked"] == "home"],
        "away": [r for r in predictions if r["picked"] == "away"],
    }

    report = {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "method": "Walk-forward predictions; no historical odds invented",
        "global": strategy_metrics(predictions),
        "strategies": {key: strategy_metrics(rows) for key, rows in strategies.items()},
        "by_league": {
            league: strategy_metrics([r for r in predictions if r["league"] == league])
            for league in sorted({r["league"] for r in predictions})
        },
    }

    output = Path("reports")
    output.mkdir(exist_ok=True)
    with open(output / "strategy_research.json", "w", encoding="utf-8") as file:
        json.dump(report, file, indent=2, ensure_ascii=False)
    pd.DataFrame(predictions).to_csv(output / "strategy_predictions.csv", index=False)
    print(json.dumps(report["strategies"], indent=2))


if __name__ == "__main__":
    main()

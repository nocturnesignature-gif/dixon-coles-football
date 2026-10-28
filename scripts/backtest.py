"""Fast walk-forward evaluation of the Dixon-Coles model."""

import json
import sys
import traceback
from collections import defaultdict
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


def load_matches():
    matches = []
    for path in LEAGUE_FILES:
        if not Path(path).exists():
            print(f"Skipping missing file: {path}")
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


def metrics(rows):
    if not rows:
        return {"matches": 0}
    probs = np.array([[r["p_home"], r["p_draw"], r["p_away"]] for r in rows])
    outcomes = np.array([
        [1.0, 0.0, 0.0] if r["actual"] == "home" else
        [0.0, 1.0, 0.0] if r["actual"] == "draw" else
        [0.0, 0.0, 1.0]
        for r in rows
    ])
    chosen = probs.argmax(axis=1)
    return {
        "matches": len(rows),
        "accuracy": float((chosen == outcomes.argmax(axis=1)).mean()),
        "log_loss": float(-np.mean(np.sum(outcomes * np.log(np.clip(probs, 1e-12, 1)), axis=1))),
        "brier_score": float(np.mean(np.sum((probs - outcomes) ** 2, axis=1))),
    }


def calibration(rows):
    buckets = []
    for low, high in [(0, 0.2), (0.2, 0.3), (0.3, 0.4), (0.4, 0.5), (0.5, 0.65), (0.65, 1.01)]:
        bucket = [r for r in rows if low <= r["top_probability"] < high]
        if not bucket:
            continue
        buckets.append({
            "range": f"{low:.0%}-{high:.0%}",
            "matches": len(bucket),
            "mean_predicted_probability": float(np.mean([r["top_probability"] for r in bucket])),
            "observed_hit_rate": float(np.mean([r["top_pick_correct"] for r in bucket])),
        })
    return buckets


def main():
    matches = load_matches()
    if len(matches) <= MIN_TRAINING_MATCHES:
        raise ValueError(f"Need more than {MIN_TRAINING_MATCHES} matches; loaded {len(matches)}")

    model_fit = None
    last_fit_size = -1
    predictions = []

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
        top = max(picks, key=picks.get)
        predictions.append({
            "date": match["date"].isoformat(),
            "league": match["league"],
            "home": match["home"],
            "away": match["away"],
            "actual": outcome,
            "p_home": picks["home"],
            "p_draw": picks["draw"],
            "p_away": picks["away"],
            "top_pick": top,
            "top_probability": picks[top],
            "top_pick_correct": int(top == outcome),
        })

        if index % 500 == 0:
            print(f"Processed {index}/{len(matches)} matches", flush=True)

    output = Path("reports")
    output.mkdir(exist_ok=True)
    pd.DataFrame(predictions).to_csv(output / "backtest_predictions.csv", index=False)

    by_league = defaultdict(list)
    for row in predictions:
        by_league[row["league"]].append(row)

    report = {
        "generated_at": datetime.now().isoformat() + "Z",
        "method": "Fast walk-forward; 800-match rolling training window",
        "refit_every_matches": REFIT_EVERY,
        "training_window": TRAINING_WINDOW,
        "global": metrics(predictions),
        "calibration": calibration(predictions),
        "by_league": {league: metrics(rows) for league, rows in sorted(by_league.items())},
    }
    with open(output / "backtest_metrics.json", "w", encoding="utf-8") as file:
        json.dump(report, file, indent=2, ensure_ascii=False)

    print(json.dumps(report["global"], indent=2), flush=True)
    print(f"Wrote {len(predictions)} predictions", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise

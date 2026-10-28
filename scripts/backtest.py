"""Walk-forward evaluation of the Dixon-Coles model across all leagues.

The script trains only on matches strictly before each evaluation date,
refits periodically to keep runtime manageable, and reports global and
per-league calibration and discrimination metrics.
"""

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from src.dixon_coles import DixonColesModel

LEAGUE_FILES = [
    "data/raw/premier_league.csv",
    "data/raw/la_liga.csv",
    "data/raw/serie_a.csv",
    "data/raw/bundesliga.csv",
    "data/raw/ligue_1.csv",
]
REFIT_EVERY = 50
MIN_TRAINING_MATCHES = 200


def load_matches() -> list[dict]:
    matches = []
    for path in LEAGUE_FILES:
        if not Path(path).exists():
            continue
        df = pd.read_csv(path)
        df = df.dropna(subset=["HomeTeam", "AwayTeam", "FTHG", "FTAG", "Date"])
        for row in df.itertuples(index=False):
            matches.append({
                "league": Path(path).stem,
                "home": str(row.HomeTeam).strip(),
                "away": str(row.AwayTeam).strip(),
                "home_goals": int(row.FTHG),
                "away_goals": int(row.FTAG),
                "date": datetime.strptime(str(row.Date), "%d/%m/%Y").date(),
            })
    matches.sort(key=lambda m: m["date"])
    return matches


def actual_outcome(match: dict) -> str:
    if match["home_goals"] > match["away_goals"]:
        return "home"
    if match["home_goals"] == match["away_goals"]:
        return "draw"
    return "away"


def metrics(rows: list[dict]) -> dict:
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
    correct = chosen == outcomes.argmax(axis=1)
    return {
        "matches": len(rows),
        "accuracy": float(correct.mean()),
        "log_loss": float(-np.mean(np.sum(outcomes * np.log(np.clip(probs, 1e-12, 1)), axis=1))),
        "brier_score": float(np.mean(np.sum((probs - outcomes) ** 2, axis=1))),
        "home_pick_rate": float((chosen == 0).mean()),
        "draw_pick_rate": float((chosen == 1).mean()),
        "away_pick_rate": float((chosen == 2).mean()),
    }


def calibration(rows: list[dict]) -> list[dict]:
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


def main() -> None:
    matches = load_matches()
    if len(matches) <= MIN_TRAINING_MATCHES:
        raise ValueError(f"Need more than {MIN_TRAINING_MATCHES} matches")

    model = DixonColesModel(half_life_days=365)
    last_fit_size = -1
    predictions = []

    for index, match in enumerate(matches):
        if index < MIN_TRAINING_MATCHES:
            continue
        if index - MIN_TRAINING_MATCHES >= last_fit_size + REFIT_EVERY or last_fit_size < 0:
            training = matches[:index]
            model.fit([m.copy() for m in training])
            last_fit_size = index

        try:
            forecast = model.predict(match["home"], match["away"])
        except KeyError:
            continue

        probabilities = forecast["probabilities"]
        outcome = actual_outcome(match)
        picks = {
            "home": probabilities["home_win"],
            "draw": probabilities["draw"],
            "away": probabilities["away_win"],
        }
        top_outcome = max(picks, key=picks.get)
        predictions.append({
            "date": match["date"].isoformat(),
            "league": match["league"],
            "home": match["home"],
            "away": match["away"],
            "actual": outcome,
            "p_home": picks["home"],
            "p_draw": picks["draw"],
            "p_away": picks["away"],
            "top_pick": top_outcome,
            "top_probability": picks[top_outcome],
            "top_pick_correct": int(top_outcome == outcome),
        })

    output = Path("reports")
    output.mkdir(exist_ok=True)
    pd.DataFrame(predictions).to_csv(output / "backtest_predictions.csv", index=False)

    by_league = defaultdict(list)
    for row in predictions:
        by_league[row["league"]].append(row)

    report = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "method": "Walk-forward; model trained only on prior matches",
        "refit_every_matches": REFIT_EVERY,
        "minimum_training_matches": MIN_TRAINING_MATCHES,
        "global": metrics(predictions),
        "calibration": calibration(predictions),
        "by_league": {league: metrics(rows) for league, rows in sorted(by_league.items())},
    }
    with open(output / "backtest_metrics.json", "w", encoding="utf-8") as file:
        json.dump(report, file, indent=2, ensure_ascii=False)

    print(json.dumps(report["global"], indent=2))
    print(f"Wrote {len(predictions)} predictions to reports/backtest_predictions.csv")


if __name__ == "__main__":
    main()

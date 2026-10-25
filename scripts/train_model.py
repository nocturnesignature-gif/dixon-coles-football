"""Train the Dixon-Coles model and save parameters."""

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from src.dixon_coles import DixonColesModel

LEAGUE_FILES = [
    "data/raw/premier_league.csv",
    "data/raw/la_liga.csv",
    "data/raw/serie_a.csv",
    "data/raw/bundesliga.csv",
    "data/raw/ligue_1.csv",
]


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
    return matches


def main() -> None:
    matches = load_matches()
    print(f"Loaded {len(matches)} matches")
    model = DixonColesModel(half_life_days=365)
    model.fit(matches)
    output = Path("models")
    output.mkdir(exist_ok=True)
    with open(output / "model_parameters.json", "w", encoding="utf-8") as file:
        json.dump(model.params_, file, indent=2, ensure_ascii=False)
    print(f"Model trained on {len(model.teams_)} teams")


if __name__ == "__main__":
    main()

"""Download historical results for the five major European leagues."""

from pathlib import Path
import io

import pandas as pd
import requests

LEAGUES = {
    "premier_league": "https://www.football-data.co.uk/mmz4281/2526/E0.csv",
    "la_liga": "https://www.football-data.co.uk/mmz4281/2526/SP1.csv",
    "serie_a": "https://www.football-data.co.uk/mmz4281/2526/I1.csv",
    "bundesliga": "https://www.football-data.co.uk/mmz4281/2526/D1.csv",
    "ligue_1": "https://www.football-data.co.uk/mmz4281/2526/F1.csv",
}


def main() -> None:
    output_dir = Path("data/raw")
    output_dir.mkdir(parents=True, exist_ok=True)

    for league, url in LEAGUES.items():
        print(f"Downloading {league}...")
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        df = pd.read_csv(io.StringIO(response.text))
        df.to_csv(output_dir / f"{league}.csv", index=False)
        print(f"Saved {len(df)} rows to data/raw/{league}.csv")


if __name__ == "__main__":
    main()

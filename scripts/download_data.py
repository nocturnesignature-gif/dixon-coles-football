"""Download historical results for the five major European leagues.

Downloads several recent seasons and consolidates them into one CSV per
league. Missing or not-yet-published season files are skipped rather than
failing the workflow.
"""

from pathlib import Path
import io

import pandas as pd
import requests

SEASONS = ["2324", "2425", "2526"]
LEAGUES = {
    "premier_league": "E0",
    "la_liga": "SP1",
    "serie_a": "I1",
    "bundesliga": "D1",
    "ligue_1": "F1",
}


def fetch_season(league_code: str, season: str) -> pd.DataFrame | None:
    url = f"https://www.football-data.co.uk/mmz4281/{season}/{league_code}.csv"
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        df = pd.read_csv(io.StringIO(response.text))
        required = {"HomeTeam", "AwayTeam", "FTHG", "FTAG", "Date"}
        if not required.issubset(df.columns):
            print(f"Skipping {season}: missing required columns")
            return None
        return df
    except (requests.RequestException, pd.errors.ParserError) as error:
        print(f"Skipping {season}: {error}")
        return None


def main() -> None:
    output_dir = Path("data/raw")
    output_dir.mkdir(parents=True, exist_ok=True)

    for league, code in LEAGUES.items():
        frames = []
        for season in SEASONS:
            print(f"Downloading {league} {season}...")
            df = fetch_season(code, season)
            if df is not None and not df.empty:
                df["Season"] = season
                frames.append(df)
                print(f"Saved {len(df)} rows for {league} {season}")

        if not frames:
            raise RuntimeError(f"No downloadable data found for {league}")

        combined = pd.concat(frames, ignore_index=True)
        combined = combined.drop_duplicates(
            subset=["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"]
        )
        combined.to_csv(output_dir / f"{league}.csv", index=False)
        print(f"Saved {len(combined)} total rows to data/raw/{league}.csv")


if __name__ == "__main__":
    main()

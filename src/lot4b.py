"""Requested competitions; registration is not evidence of provider coverage."""

LOT4B = (
    ("ligue_2", "Ligue 2", "domestic_league"),
    ("la_liga_2", "LaLiga 2", "domestic_league"),
    ("serie_b", "Serie B", "domestic_league"),
    ("bundesliga_2", "Bundesliga 2", "domestic_league"),
    ("liga_portugal_2", "D2 Portugal", "domestic_league"),
    ("champions_league", "Champions League", "international_clubs"),
    ("europa_league", "Europa League", "international_clubs"),
    ("conference_league", "Conference League", "international_clubs"),
    ("nations_league", "Ligue des nations", "national_teams"),
)


def requested_competitions():
    return [
        {
            "competition": key,
            "label": label,
            "category": category,
            "provider_id": None,
            "coverage_verified": False,
            "integration_status": "pending_provider_verification",
            "forecast_enabled": False,
        }
        for key, label, category in LOT4B
    ]


def validate_registry():
    keys = [key for key, _, _ in LOT4B]
    if len(keys) != 9 or len(set(keys)) != 9:
        raise ValueError("Lot 4b must contain nine distinct competitions")
    return True

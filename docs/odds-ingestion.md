# Odds ingestion v1

Ce module enregistre les cotes historiques observees dans `historical_match_odds`.

Il est strictement analytique : il n cree aucun pari, stake, combiné ou alerte.

## Format d’entrée

```json
[
  {
    "match_date": "2026-11-01",
    "league": "Premier League",
    "home_team": "Arsenal",
    "away_team": "Chelsea",
    "market": "home",
    "bookmaker": "pinnacle",
    "odds_type": "prediction",
    "odds": 2.05,
    "captured_at": "2026-11-01T09:00:00Z",
    "source": "odds-provider",
    "model_version": "dixon_coles_walkforward_v1"
  }
]
```

## Utilisation

Validation sans ecriture :

```bash
python scripts/ingest_historical_odds.py odds.json --dry-run
```

Ingestion reelle :

```bash
python scripts/ingest_historical_odds.py odds.json
```

Variables requises :

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

## Regles

- Une ligne est creee par couple `match + marche + bookmaker + odds_type`.
- `odds_type` doit valoir `prediction` ou `closing`.
- La cote doit etre superieure a 1.
- L’upsert est idempotent : relancer le fichier ne duplique pas les observations.
- La cote de cloture n’est jamais deduite : elle doit etre capturee separement.

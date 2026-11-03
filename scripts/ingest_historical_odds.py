"""Idempotent ingestion of historical football odds into Supabase.

Read-only with respect to betting: this script records observed prices only.
"""
import argparse
import json
import os
import urllib.error
import urllib.request
from datetime import datetime

VALID_MARKETS = {"home", "draw", "away"}
VALID_ODDS_TYPES = {"prediction", "closing"}


class IngestionError(Exception):
    pass


def supabase_request(method, path, payload=None):
    base = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base or not key:
        raise IngestionError("missing_supabase_config")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{base}/rest/v1/{path}",
        data=data,
        method=method,
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=representation",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read().decode()
            return json.loads(body) if body else []
    except urllib.error.HTTPError as exc:
        raise IngestionError(f"supabase_http_{exc.code}: {exc.read().decode()}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise IngestionError("supabase_unavailable") from exc


def normalize_row(row):
    required = [
        "match_date", "league", "home_team", "away_team",
        "market", "bookmaker", "odds_type", "odds", "captured_at",
    ]
    missing = [field for field in required if not row.get(field)]
    if missing:
        raise IngestionError(f"missing_fields: {', '.join(missing)}")
    if row["market"] not in VALID_MARKETS:
        raise IngestionError(f"invalid_market: {row['market']}")
    if row["odds_type"] not in VALID_ODDS_TYPES:
        raise IngestionError(f"invalid_odds_type: {row['odds_type']}")
    try:
        odds = float(row["odds"])
    except (TypeError, ValueError) as exc:
        raise IngestionError("invalid_odds") from exc
    if odds <= 1:
        raise IngestionError("odds_must_be_greater_than_one")
    datetime.fromisoformat(row["captured_at"].replace("Z", "+00:00"))
    return {
        "match_date": row["match_date"],
        "league": row["league"],
        "home_team": row["home_team"],
        "away_team": row["away_team"],
        "market": row["market"],
        "bookmaker": row["bookmaker"],
        "odds_type": row["odds_type"],
        "odds": odds,
        "captured_at": row["captured_at"],
        "source": row.get("source"),
        "model_version": row.get("model_version"),
    }


def ingest(rows, dry_run=False):
    normalized = []
    errors = []
    for index, row in enumerate(rows):
        try:
            normalized.append(normalize_row(row))
        except IngestionError as exc:
            errors.append({"index": index, "error": str(exc), "row": row})
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "valid_rows": len(normalized),
            "invalid_rows": len(errors),
            "errors": errors,
        }
    if normalized:
        supabase_request(
            "POST",
            "historical_match_odds?on_conflict=match_date,league,home_team,away_team,market,bookmaker,odds_type",
            normalized,
        )
    return {
        "ok": True,
        "dry_run": False,
        "submitted_rows": len(normalized),
        "invalid_rows": len(errors),
        "errors": errors,
    }


def load_rows(path):
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    rows = payload.get("rows", payload) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise IngestionError("input_must_be_a_json_array_or_object_with_rows")
    return rows


def main():
    parser = argparse.ArgumentParser(description="Ingest historical football odds")
    parser.add_argument("input", help="JSON file containing an array of odds observations")
    parser.add_argument("--dry-run", action="store_true", help="Validate without writing to Supabase")
    args = parser.parse_args()
    try:
        result = ingest(load_rows(args.input), dry_run=args.dry_run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (IngestionError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Generate an experimental prospective tracking report.

Reads the Supabase views:
- football_experimental_forecasts_v4
- football_model_prospective_results_v4

This report is experimental and unvalidated. It does not create bets,
calculate stakes or edges, or approve any model.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
API_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

FORECAST_VIEW = "football_experimental_forecasts_v4"
RESULTS_VIEW = "football_model_prospective_results_v4"


def require_config() -> None:
    if not BASE_URL or not API_KEY:
        raise SystemExit(
            "Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY before running this script."
        )


def fetch_view(view: str) -> list[dict]:
    url = f"{BASE_URL}/rest/v1/{view}?select=*&order=kickoff_at.asc"
    request = urllib.request.Request(
        url,
        headers={
            "apikey": API_KEY,
            "Authorization": f"Bearer {API_KEY}",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(f"Supabase returned HTTP {response.status} for {view}")
        return json.load(response)


def format_percent(value: float | None) -> str:
    return "—" if value is None else f"{float(value) * 100:.1f}%"


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    require_config()
    generated_at = datetime.now(timezone.utc)
    forecasts = fetch_view(FORECAST_VIEW)
    results = fetch_view(RESULTS_VIEW)

    report_dir = Path("reports")
    report_dir.mkdir(exist_ok=True)
    stamp = generated_at.strftime("%Y%m%d-%H%M%S")
    markdown_path = report_dir / f"prospective-tracking-{stamp}.md"
    forecasts_csv = report_dir / f"prospective-forecasts-{stamp}.csv"
    results_csv = report_dir / f"prospective-results-{stamp}.csv"

    write_csv(forecasts_csv, forecasts)
    write_csv(results_csv, results)

    lines = [
        "# Suivi prospectif expérimental — Ligue 1",
        "",
        f"Généré le : {generated_at.isoformat()}",
        "",
        "**Statut : expérimental — non validé.** Ce rapport ne constitue pas une",
        "sélection de pari et n’approuve aucun modèle.",
        "",
        f"- Prédictions expérimentales : **{len(forecasts)}**",
        f"- Résultats rapprochés : **{len(results)}**",
        "",
        "## Prédictions avant-match",
        "",
        "| Match | Coup d’envoi | Dom. | Nul | Ext. | Éligibilité |",
        "|---|---:|---:|---:|---:|---|",
    ]

    for item in forecasts:
        match = f"{item['home_team']} — {item['away_team']}"
        kickoff = item.get("kickoff_at", "—")
        lines.append(
            f"| {match} | {kickoff} | {format_percent(item.get('home_probability'))} | "
            f"{format_percent(item.get('draw_probability'))} | "
            f"{format_percent(item.get('away_probability'))} | {item.get('eligibility', '—')} |"
        )

    lines += ["", "## Résultats rapprochés", ""]
    if results:
        lines += [
            "| Match | Score | Résultat réel | Choix le plus probable correct | Log loss | Brier |",
            "|---|---:|---|---|---:|---:|",
        ]
        for item in results:
            match = f"{item['home_team']} — {item['away_team']}"
            score = f"{item['home_score']}–{item['away_score']}"
            lines.append(
                f"| {match} | {score} | {item['actual_outcome']} | "
                f"{'oui' if item['top_pick_correct'] else 'non'} | "
                f"{float(item['log_loss']):.3f} | {float(item['brier_score']):.3f} |"
            )
        accuracy = sum(1 for item in results if item["top_pick_correct"]) / len(results)
        mean_log_loss = sum(float(item["log_loss"]) for item in results) / len(results)
        mean_brier = sum(float(item["brier_score"]) for item in results) / len(results)
        lines += [
            "",
            f"- Précision du choix le plus probable : **{accuracy * 100:.1f}%**",
            f"- Log loss moyen : **{mean_log_loss:.3f}**",
            f"- Brier score moyen : **{mean_brier:.3f}**",
        ]
    else:
        lines.append("Aucun résultat final n’est encore disponible.")

    lines += [
        "",
        "## Limites",
        "",
        "- Prédictions expérimentales avant-match, pas des prédictions en direct.",
        "- Les modèles restent candidats et les prédictions restent non vérifiées.",
        "- Aucune sélection de pari, edge, mise Kelly ou règlement de journal.",
        "- Les scores proviennent d’observations fournisseur et ne sont pas une confirmation indépendante.",
    ]

    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Report: {markdown_path}")
    print(f"Forecasts CSV: {forecasts_csv}")
    print(f"Results CSV: {results_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

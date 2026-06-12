"""
Sincroniza partidos y resultados del Mundial 2026 desde football-data.org
hacia la tabla `matches` de Supabase.

Se ejecuta como GitHub Action cada 15 minutos.
La primera ejecución siembra los 104 partidos; las siguientes actualizan
marcadores y estados.

Variables de entorno requeridas:
  FOOTBALL_DATA_TOKEN   -> token gratuito de https://www.football-data.org/client/register
  SUPABASE_URL          -> https://<tu-proyecto>.supabase.co
  SUPABASE_SERVICE_KEY  -> service_role key (Settings > API). NUNCA la pongas en el frontend.

Dependencias: requests  (pip install requests)
"""

import os
import sys
import requests
from datetime import datetime, timezone

FD_TOKEN = os.environ["FOOTBALL_DATA_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

FD_URL = "https://api.football-data.org/v4/competitions/WC/matches"


def fetch_matches() -> list[dict]:
    resp = requests.get(FD_URL, headers={"X-Auth-Token": FD_TOKEN}, timeout=30)
    resp.raise_for_status()
    return resp.json()["matches"]


def to_row(m: dict) -> dict:
    stage = m.get("stage", "")
    # Defensive: fullTime puede ser null o ausente cuando el API aún no confirma el marcador
    full_time = (m.get("score") or {}).get("fullTime") or {}
    home_score = full_time.get("home")
    away_score = full_time.get("away")

    row = {
        "id": m["id"],
        "stage": stage,
        "phase": "grupos" if stage == "GROUP_STAGE" else "eliminatorias",
        "group_name": m.get("group"),
        "home_team": m["homeTeam"].get("name") or "Por definir",
        "away_team": m["awayTeam"].get("name") or "Por definir",
        "home_crest": m["homeTeam"].get("crest"),
        "away_crest": m["awayTeam"].get("crest"),
        "kickoff": m["utcDate"],
        "status": m.get("status", "SCHEDULED"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    # Solo incluir marcadores cuando el API los devuelve; si son null no
    # sobreescribimos un marcador ya guardado correctamente en la BD.
    if home_score is not None:
        row["home_score"] = home_score
    if away_score is not None:
        row["away_score"] = away_score

    return row


def _post(rows: list[dict]) -> None:
    """Envía un batch a Supabase; todos los rows deben tener las mismas claves."""
    if not rows:
        return
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/matches?on_conflict=id",
        headers={
            "apikey": SERVICE_KEY,
            "Authorization": f"Bearer {SERVICE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        json=rows,
        timeout=30,
    )
    if resp.status_code >= 300:
        print(f"Error de Supabase {resp.status_code}: {resp.text}", file=sys.stderr)
        sys.exit(1)


def upsert(rows: list[dict]) -> None:
    # Llamada 1: todos los campos excepto marcadores (claves uniformes en todos los rows)
    SCORE_KEYS = {"home_score", "away_score"}
    base_rows = [{k: v for k, v in r.items() if k not in SCORE_KEYS} for r in rows]
    _post(base_rows)

    # Llamada 2: solo los partidos donde el API devolvió marcador confirmado
    scored_rows = [
        {"id": r["id"], "home_score": r["home_score"], "away_score": r["away_score"]}
        for r in rows if "home_score" in r
    ]
    _post(scored_rows)


def main() -> None:
    matches = fetch_matches()
    rows = [to_row(m) for m in matches]
    upsert(rows)

    finished = [r for r in rows if r["status"] == "FINISHED"]
    with_score = [r for r in finished if "home_score" in r]
    print(f"Sincronizados {len(rows)} partidos.")
    print(f"  Finalizados: {len(finished)} | Con marcador: {len(with_score)}")
    for r in finished:
        score = f"{r['home_score']}-{r['away_score']}" if "home_score" in r else "sin marcador aún"
        print(f"  [{score}] {r['home_team']} vs {r['away_team']}")


if __name__ == "__main__":
    main()

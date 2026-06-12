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


def to_row(m: dict) -> dict | None:
    stage = m.get("stage") or ""
    kickoff = m.get("utcDate")

    # Saltar partidos placeholder que el API devuelve sin datos esenciales
    if not stage or not kickoff:
        return None

    # Defensive: fullTime puede ser null o ausente cuando el API aún no confirma el marcador
    full_time = (m.get("score") or {}).get("fullTime") or {}
    home_score = full_time.get("home")
    away_score = full_time.get("away")

    # Todos los rows siempre incluyen home_score y away_score (null si el API aún no los tiene).
    # PostgREST requiere claves uniformes en todo el batch; mezclar rows con/sin esas claves
    # produce PGRST102. Una vez que el API devuelve el marcador real, el siguiente run lo guarda.
    return {
        "id": m["id"],
        "stage": stage,
        "phase": "grupos" if stage == "GROUP_STAGE" else "eliminatorias",
        "group_name": m.get("group"),
        "home_team": (m.get("homeTeam") or {}).get("name") or "Por definir",
        "away_team": (m.get("awayTeam") or {}).get("name") or "Por definir",
        "home_crest": (m.get("homeTeam") or {}).get("crest"),
        "away_crest": (m.get("awayTeam") or {}).get("crest"),
        "kickoff": kickoff,
        "status": m.get("status") or "SCHEDULED",
        "home_score": home_score,
        "away_score": away_score,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def upsert(rows: list[dict]) -> None:
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


def main() -> None:
    matches = fetch_matches()
    all_rows = [to_row(m) for m in matches]
    rows = [r for r in all_rows if r is not None]
    skipped = len(all_rows) - len(rows)
    if skipped:
        print(f"Saltados {skipped} partidos sin datos esenciales (placeholders del API).")
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

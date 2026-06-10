"""
Sincroniza partidos y resultados del Mundial 2026 desde football-data.org
hacia la tabla `matches` de Supabase.

Se ejecuta como Cron Job en Render (cada 15 min recomendado).
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
    score = m.get("score", {}).get("fullTime", {})
    return {
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
        "home_score": score.get("home"),
        "away_score": score.get("away"),
        "updated_at": "now()",
    }


def upsert(rows: list[dict]) -> None:
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
    rows = [to_row(m) for m in matches]
    upsert(rows)
    finished = sum(1 for r in rows if r["status"] == "FINISHED")
    print(f"Sincronizados {len(rows)} partidos ({finished} finalizados).")


if __name__ == "__main__":
    main()

"""
Sincroniza los TIROS DE ESQUINA del Mundial 2026 desde API-Football (api-sports.io)
hacia las columnas `home_corners` / `away_corners` de la tabla `matches` de Supabase.

Es un proceso INDEPENDIENTE de update_results.py:
  - update_results.py  -> marcadores y estados (football-data.org)
  - update_corners.py  -> SOLO tiros de esquina (API-Football)

Como los IDs de partido difieren entre los dos proveedores, este script MACHEA cada
partido por fecha (UTC) + nombres de equipos normalizados. Si un partido no machea,
lo reporta en el log para agregar un alias y nunca pisa datos de marcador.

Variables de entorno requeridas:
  API_FOOTBALL_KEY      -> API key de https://dashboard.api-football.com (plan Free: 100 req/día)
  SUPABASE_URL          -> https://<tu-proyecto>.supabase.co
  SUPABASE_SERVICE_KEY  -> service_role key (Settings > API). NUNCA en el frontend.

Dependencias: requests  (pip install requests)
"""

import os
import sys
import time
import unicodedata
import requests

API_KEY = os.environ["API_FOOTBALL_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

AF_BASE = "https://v3.football.api-sports.io"
WC_LEAGUE_ID = 1        # FIFA World Cup en API-Football
WC_SEASON = 2026

# Diferencias conocidas de nombres entre football-data.org (nuestra BD) y API-Football.
# Las claves/valores van en forma YA normalizada (sin acentos, minúsculas, sin puntuación).
ALIASES = {
    "korea republic": "south korea",
    "republic of korea": "south korea",
    "korea dpr": "north korea",
    "ir iran": "iran",
    "united states": "usa",
    "cote divoire": "ivory coast",
    "czechia": "czech republic",
    "turkiye": "turkey",
    "cabo verde": "cape verde",
    "china pr": "china",
    "bosnia and herzegovina": "bosnia",
}


def normalize(name: str) -> str:
    """Minúsculas, sin acentos ni puntuación, espacios colapsados, con alias aplicado."""
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", name)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = n.lower()
    n = "".join(c if c.isalnum() or c.isspace() else " " for c in n)
    n = " ".join(n.split())
    return ALIASES.get(n, n)


def match_key(home: str, away: str, kickoff_iso: str) -> tuple:
    """Clave de macheo: fecha UTC + conjunto de equipos normalizados (orden-independiente)."""
    day = (kickoff_iso or "")[:10]
    return (day, frozenset({normalize(home), normalize(away)}))


# ---------- Supabase ----------
def sb_headers() -> dict:
    return {
        "apikey": SERVICE_KEY,
        "Authorization": f"Bearer {SERVICE_KEY}",
        "Content-Type": "application/json",
    }


def fetch_matches_pendientes() -> list[dict]:
    """Partidos terminados que aún no tienen corners."""
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/matches",
        headers=sb_headers(),
        params={
            "select": "id,home_team,away_team,kickoff,status,home_corners",
            "status": "eq.FINISHED",
            "home_corners": "is.null",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def patch_corners(match_id: int, home_corners: int, away_corners: int) -> None:
    resp = requests.patch(
        f"{SUPABASE_URL}/rest/v1/matches",
        headers={**sb_headers(), "Prefer": "return=minimal"},
        params={"id": f"eq.{match_id}"},
        json={"home_corners": home_corners, "away_corners": away_corners},
        timeout=30,
    )
    if resp.status_code >= 300:
        print(f"  Error Supabase {resp.status_code} al guardar {match_id}: {resp.text}", file=sys.stderr)


# ---------- API-Football ----------
def af_headers() -> dict:
    return {"x-apisports-key": API_KEY}


def fetch_af_fixtures() -> list[dict]:
    resp = requests.get(
        f"{AF_BASE}/fixtures",
        headers=af_headers(),
        params={"league": WC_LEAGUE_ID, "season": WC_SEASON},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        print(f"API-Football devolvió errores en /fixtures: {data['errors']}", file=sys.stderr)
        sys.exit(1)
    return data.get("response", [])


def fetch_af_corners(fixture_id: int) -> tuple[int | None, int | None]:
    """Devuelve (corners_local, corners_visita) según el orden home/away del fixture, o (None, None)."""
    resp = requests.get(
        f"{AF_BASE}/fixtures/statistics",
        headers=af_headers(),
        params={"fixture": fixture_id},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    teams = data.get("response", [])
    corners_by_team = {}
    for t in teams:
        team_id = (t.get("team") or {}).get("id")
        val = None
        for st in t.get("statistics", []):
            if st.get("type") == "Corner Kicks":
                val = st.get("value")
                break
        corners_by_team[team_id] = val
    return corners_by_team


def main() -> None:
    pendientes = fetch_matches_pendientes()
    if not pendientes:
        print("No hay partidos terminados pendientes de corners. Nada que hacer.")
        return

    print(f"{len(pendientes)} partido(s) terminado(s) sin corners. Buscando en API-Football…")
    fixtures = fetch_af_fixtures()
    print(f"API-Football devolvió {len(fixtures)} fixtures del Mundial.")

    # Índice de fixtures por clave de macheo
    index = {}
    for fx in fixtures:
        home = ((fx.get("teams") or {}).get("home") or {}).get("name", "")
        away = ((fx.get("teams") or {}).get("away") or {}).get("name", "")
        date = (fx.get("fixture") or {}).get("date", "")
        index[match_key(home, away, date)] = fx

    actualizados = 0
    no_macheados = []
    sin_stats = []

    for m in pendientes:
        key = match_key(m["home_team"], m["away_team"], m["kickoff"])
        fx = index.get(key)
        if not fx:
            no_macheados.append(m)
            continue

        fixture = fx.get("fixture") or {}
        fixture_id = fixture.get("id")
        af_home_id = ((fx.get("teams") or {}).get("home") or {}).get("id")
        af_away_id = ((fx.get("teams") or {}).get("away") or {}).get("id")

        corners_by_team = fetch_af_corners(fixture_id)
        time.sleep(1)  # cortesía con el rate limit del plan Free

        h = corners_by_team.get(af_home_id)
        a = corners_by_team.get(af_away_id)
        if h is None or a is None:
            sin_stats.append(m)
            continue

        # Nuestro home/away puede estar invertido respecto al de API-Football: alineamos por nombre.
        if normalize(m["home_team"]) == normalize(((fx.get("teams") or {}).get("away") or {}).get("name", "")):
            h, a = a, h

        patch_corners(m["id"], h, a)
        actualizados += 1
        print(f"  ✓ {m['home_team']} {h}–{a} {m['away_team']} (corners)")

    print(f"\nActualizados con corners: {actualizados}")
    if sin_stats:
        print(f"Sin estadísticas de corners todavía (reintentará luego): {len(sin_stats)}")
    if no_macheados:
        print(f"No macheados ({len(no_macheados)}) — revisa nombres y agrega un alias en ALIASES:")
        for m in no_macheados:
            print(f"  - {m['home_team']} vs {m['away_team']} ({m['kickoff'][:10]})")


if __name__ == "__main__":
    main()

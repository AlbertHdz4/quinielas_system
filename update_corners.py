"""
Sincroniza datos extra del Mundial 2026 desde API-Football (api-sports.io) hacia la
tabla `matches` de Supabase:
  - `home_corners` / `away_corners` -> tiros de esquina (endpoint /fixtures/statistics)
  - `home_yellows` / `away_yellows` -> tarjetas amarillas (misma llamada de estadísticas)
  - `went_penalties`                -> si el partido se definió en penales (status 'PEN'
                                        del endpoint /fixtures, SIN request adicional)

Es un proceso INDEPENDIENTE de update_results.py:
  - update_results.py  -> marcadores y estados (football-data.org)
  - update_corners.py  -> corners y penales (API-Football)

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
    """Partidos de ELIMINATORIAS terminados a los que les falta corners o el dato de penales.
    Se limita a eliminatorias porque las apuestas extra (corners/penales) solo existen ahí;
    así evitamos gastar requests en los ~72 partidos de fase de grupos."""
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/matches",
        headers=sb_headers(),
        params={
            "select": "id,home_team,away_team,kickoff,status,home_corners,home_yellows,went_penalties",
            "status": "eq.FINISHED",
            "phase": "eq.eliminatorias",
            "or": "(home_corners.is.null,home_yellows.is.null,went_penalties.is.null)",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def patch_match(match_id: int, fields: dict) -> None:
    if not fields:
        return
    resp = requests.patch(
        f"{SUPABASE_URL}/rest/v1/matches",
        headers={**sb_headers(), "Prefer": "return=minimal"},
        params={"id": f"eq.{match_id}"},
        json=fields,
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


def fetch_af_stats(fixture_id: int) -> dict:
    """Devuelve {team_id: {'corners': int|None, 'yellows': int|None}} para un partido.
    Una sola llamada trae ambas estadísticas (corners y amarillas)."""
    resp = requests.get(
        f"{AF_BASE}/fixtures/statistics",
        headers=af_headers(),
        params={"fixture": fixture_id},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    stats_by_team = {}
    for t in data.get("response", []):
        team_id = (t.get("team") or {}).get("id")
        corners = yellows = None
        for st in t.get("statistics", []):
            if st.get("type") == "Corner Kicks":
                corners = st.get("value")
            elif st.get("type") == "Yellow Cards":
                yellows = st.get("value")
        stats_by_team[team_id] = {"corners": corners, "yellows": yellows}
    return stats_by_team


# Estados "terminados" de API-Football. 'PEN' = se definió en penales.
FINISHED_SHORT = {"FT", "AET", "PEN", "AWD", "WO"}


def main() -> None:
    pendientes = fetch_matches_pendientes()
    if not pendientes:
        print("No hay partidos terminados pendientes de corners ni penales. Nada que hacer.")
        return

    print(f"{len(pendientes)} partido(s) terminado(s) pendientes. Buscando en API-Football…")
    fixtures = fetch_af_fixtures()
    print(f"API-Football devolvió {len(fixtures)} fixtures del Mundial.")

    # Índice de fixtures por clave de macheo
    index = {}
    for fx in fixtures:
        home = ((fx.get("teams") or {}).get("home") or {}).get("name", "")
        away = ((fx.get("teams") or {}).get("away") or {}).get("name", "")
        date = (fx.get("fixture") or {}).get("date", "")
        index[match_key(home, away, date)] = fx

    corners_ok = 0
    yellows_ok = 0
    pen_ok = 0
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
        status_short = ((fixture.get("status") or {}).get("short"))
        af_home_id = ((fx.get("teams") or {}).get("home") or {}).get("id")
        af_away_name = ((fx.get("teams") or {}).get("away") or {}).get("name", "")
        # Nuestro home/away puede estar invertido respecto al de API-Football.
        inverted = normalize(m["home_team"]) == normalize(af_away_name)

        patch: dict = {}

        # 1) Penales: gratis, sale del status del fixture (sin request extra).
        if m.get("went_penalties") is None and status_short in FINISHED_SHORT:
            patch["went_penalties"] = (status_short == "PEN")

        # 2) Corners y amarillas: una sola llamada de estadísticas trae ambos.
        if m.get("home_corners") is None or m.get("home_yellows") is None:
            stats = fetch_af_stats(fixture_id)
            time.sleep(1)  # cortesía con el rate limit del plan Free
            home_st = stats.get(af_home_id, {})
            away_st = stats.get(af_away_id, {})
            falto_algo = False

            if m.get("home_corners") is None:
                hc, ac = home_st.get("corners"), away_st.get("corners")
                if hc is None or ac is None:
                    falto_algo = True
                else:
                    if inverted: hc, ac = ac, hc
                    patch["home_corners"], patch["away_corners"] = hc, ac

            if m.get("home_yellows") is None:
                hy, ay = home_st.get("yellows"), away_st.get("yellows")
                if hy is None or ay is None:
                    falto_algo = True
                else:
                    if inverted: hy, ay = ay, hy
                    patch["home_yellows"], patch["away_yellows"] = hy, ay

            if falto_algo:
                sin_stats.append(m)

        if not patch:
            continue

        patch_match(m["id"], patch)
        if "home_corners" in patch:
            corners_ok += 1
            print(f"  ✓ {m['home_team']} {patch['home_corners']}–{patch['away_corners']} {m['away_team']} (corners)")
        if "home_yellows" in patch:
            yellows_ok += 1
            print(f"  ✓ {m['home_team']} {patch['home_yellows']}–{patch['away_yellows']} {m['away_team']} (amarillas)")
        if "went_penalties" in patch:
            pen_ok += 1
            print(f"  ✓ {m['home_team']} vs {m['away_team']} — penales: {'SÍ' if patch['went_penalties'] else 'no'}")

    print(f"\nActualizados — corners: {corners_ok} | amarillas: {yellows_ok} | penales: {pen_ok}")
    if sin_stats:
        print(f"Sin estadísticas completas todavía (reintentará luego): {len(sin_stats)}")
    if no_macheados:
        print(f"No macheados ({len(no_macheados)}) — revisa nombres y agrega un alias en ALIASES:")
        for m in no_macheados:
            print(f"  - {m['home_team']} vs {m['away_team']} ({m['kickoff'][:10]})")


if __name__ == "__main__":
    main()

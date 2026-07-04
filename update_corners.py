"""
Sincroniza datos extra del Mundial 2026 desde la API pública (no oficial) de ESPN
hacia la tabla `matches` de Supabase:
  - `home_corners` / `away_corners` -> tiros de esquina (stat `wonCorners`)
  - `home_yellows` / `away_yellows` -> tarjetas amarillas (eventos `yellowCard` del play-by-play)
  - `went_penalties`                -> si el partido se definió en penales (status STATUS_FINAL_PEN)

Es un proceso INDEPENDIENTE de update_results.py:
  - update_results.py  -> marcadores y estados (football-data.org)
  - update_corners.py  -> corners, amarillas y penales (ESPN)

ESPN expone un JSON público sin llave (site.api.espn.com). Es una API NO documentada:
es gratuita y cubre el Mundial 2026, pero ESPN podría cambiarla sin aviso. Si algún día
deja de funcionar, el log avisará y se puede ajustar el parser o cargar los datos a mano.

Como los IDs difieren de football-data.org, se MACHEA por fecha (UTC) + nombres de equipos
normalizados. Si un partido no machea, se reporta para agregar un alias; nunca pisa marcadores.

Variables de entorno requeridas:
  SUPABASE_URL          -> https://<tu-proyecto>.supabase.co
  SUPABASE_SERVICE_KEY  -> service_role key (Settings > API). NUNCA en el frontend.

Dependencias: requests  (pip install requests)
"""

import os
import sys
import unicodedata
import requests
from datetime import datetime, timedelta

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
UA = "Mozilla/5.0 (compatible; QuinielaBot/1.0)"

# Diferencias conocidas de nombres entre football-data.org (nuestra BD) y ESPN.
# Las claves/valores van en forma YA normalizada (sin acentos, minúsculas, sin puntuación).
# normalize() aplica el alias a AMBAS fuentes, así convergen al mismo nombre canónico.
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
    "cape verde islands": "cape verde",
    "china pr": "china",
    "bosnia and herzegovina": "bosnia",
}


def normalize(name: str) -> str:
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", name)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = n.lower()
    n = "".join(c if c.isalnum() or c.isspace() else " " for c in n)
    n = " ".join(n.split())
    return ALIASES.get(n, n)


def match_key(home: str, away: str, day: str) -> tuple:
    """Clave de macheo: fecha UTC (YYYY-MM-DD) + conjunto de equipos (orden-independiente)."""
    return (day[:10], frozenset({normalize(home), normalize(away)}))


# ---------- Supabase ----------
def sb_headers() -> dict:
    return {
        "apikey": SERVICE_KEY,
        "Authorization": f"Bearer {SERVICE_KEY}",
        "Content-Type": "application/json",
    }


def fetch_matches_pendientes() -> list[dict]:
    """Partidos de ELIMINATORIAS terminados a los que les falta corners, amarillas o penales."""
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


# ---------- ESPN ----------
def fetch_espn_events(date_param: str) -> list[dict]:
    resp = requests.get(
        ESPN_URL,
        params={"dates": date_param, "limit": 300},
        headers={"User-Agent": UA},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("events", [])


def _stat(competitor: dict, name: str):
    for s in competitor.get("statistics", []):
        if s.get("name") == name:
            try:
                return int(s.get("displayValue"))
            except (TypeError, ValueError):
                return None
    return None


def parse_event(ev: dict) -> dict | None:
    comp = (ev.get("competitions") or [{}])[0]
    st = (comp.get("status") or {}).get("type") or {}
    completed = st.get("completed") is True
    competitors = comp.get("competitors") or []
    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away = next((c for c in competitors if c.get("homeAway") == "away"), None)
    if not home or not away:
        return None

    def tid(c):
        i = (c.get("team") or {}).get("id")
        return str(i) if i is not None else None

    # Amarillas: contar eventos yellowCard del play-by-play, por equipo.
    details = comp.get("details") or []
    yellows = {}
    for d in details:
        if d.get("yellowCard") is True:
            t = (d.get("team") or {}).get("id")
            t = str(t) if t is not None else None
            yellows[t] = yellows.get(t, 0) + 1
    # En un partido terminado, la ausencia de tarjeta = 0 (no "dato faltante").
    hy = yellows.get(tid(home), 0) if (completed and details) else None
    ay = yellows.get(tid(away), 0) if (completed and details) else None

    return {
        "date": ev.get("date", ""),
        "home_name": (home.get("team") or {}).get("displayName", ""),
        "away_name": (away.get("team") or {}).get("displayName", ""),
        "home_corners": _stat(home, "wonCorners"),
        "away_corners": _stat(away, "wonCorners"),
        "home_yellows": hy,
        "away_yellows": ay,
        "went_penalties": (st.get("name") == "STATUS_FINAL_PEN") if completed else None,
        "completed": completed,
    }


def main() -> None:
    pendientes = fetch_matches_pendientes()
    if not pendientes:
        print("No hay partidos de eliminatorias pendientes de stats. Nada que hacer.")
        return

    # ESPN agrupa los eventos por fecha en horario de EE.UU., así que un partido a primera
    # hora UTC puede caer en el "bucket" del día anterior. Ampliamos el rango ±1 día para
    # asegurarnos de traerlo; el macheo usa la fecha real (UTC) del evento, no la del bucket.
    dias = sorted({m["kickoff"][:10] for m in pendientes})
    d_min = (datetime.fromisoformat(dias[0])  - timedelta(days=1)).strftime("%Y%m%d")
    d_max = (datetime.fromisoformat(dias[-1]) + timedelta(days=1)).strftime("%Y%m%d")
    date_param = f"{d_min}-{d_max}"
    print(f"{len(pendientes)} partido(s) pendientes. Consultando ESPN ({date_param})…")

    events = fetch_espn_events(date_param)
    print(f"ESPN devolvió {len(events)} partidos en ese rango.")

    index = {}
    for ev in events:
        p = parse_event(ev)
        if p:
            index[match_key(p["home_name"], p["away_name"], p["date"])] = p

    corners_ok = yellows_ok = pen_ok = 0
    no_macheados = []
    sin_stats = []

    for m in pendientes:
        p = index.get(match_key(m["home_team"], m["away_team"], m["kickoff"]))
        if not p:
            no_macheados.append(m)
            continue
        if not p["completed"]:
            sin_stats.append(m)
            continue

        # Nuestro home/away puede estar invertido respecto al de ESPN: alineamos por nombre.
        inverted = normalize(m["home_team"]) == normalize(p["away_name"])
        hc, ac = (p["away_corners"], p["home_corners"]) if inverted else (p["home_corners"], p["away_corners"])
        hy, ay = (p["away_yellows"], p["home_yellows"]) if inverted else (p["home_yellows"], p["away_yellows"])

        patch: dict = {}
        if m.get("went_penalties") is None and p["went_penalties"] is not None:
            patch["went_penalties"] = p["went_penalties"]
        if m.get("home_corners") is None and hc is not None and ac is not None:
            patch["home_corners"], patch["away_corners"] = hc, ac
        if m.get("home_yellows") is None and hy is not None and ay is not None:
            patch["home_yellows"], patch["away_yellows"] = hy, ay

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
        print(f"Aún no finalizan en ESPN (reintentará luego): {len(sin_stats)}")
    if no_macheados:
        print(f"No macheados ({len(no_macheados)}) — revisa nombres y agrega un alias en ALIASES:")
        for m in no_macheados:
            print(f"  - {m['home_team']} vs {m['away_team']} ({m['kickoff'][:10]})")


if __name__ == "__main__":
    main()

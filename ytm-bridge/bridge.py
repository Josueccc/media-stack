#!/usr/bin/env python3
"""
ytm-bridge - publica tus recomendaciones de YouTube Music como una
Custom List de Lidarr.

Cadena completa:
    ytmusicapi -> nombres de artista -> /api/v1/artist/lookup de Lidarr
    -> MusicBrainz IDs -> JSON servido en /artists.json

Lidarr (Settings -> Import Lists -> Custom List) solo entiende MBIDs de
artista, así que el trabajo real de este puente es resolver nombres.
La resolución se cachea en disco: YTM devuelve casi siempre los mismos
artistas y el metadata server de Lidarr no merece esa paliza.
"""

import csv
import json
import os
import re
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from flask import Flask, jsonify, send_file
from waitress import serve as waitress_serve


# ---------------------------------------------------------------
# Configuración (todo por env, igual que el resto del stack)
# ---------------------------------------------------------------
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


LIDARR_URL = os.environ.get("LIDARR_URL", "http://lidarr:8686").rstrip("/")
LIDARR_API_KEY = os.environ.get("LIDARR_API_KEY", "").strip()

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
TAKEOUT_DIR = Path(os.environ.get("TAKEOUT_DIR", str(DATA_DIR / "takeout")))

# home = el feed personalizado (lo interesante), el resto es tu biblioteca.
SOURCES = [s.strip() for s in os.environ.get(
    "YTM_SOURCES", "home,library,subscriptions,liked").split(",") if s.strip()]

HOME_ROWS = _env_int("YTM_HOME_ROWS", 10)
LIBRARY_LIMIT = _env_int("YTM_LIBRARY_LIMIT", 200)
LIKED_LIMIT = _env_int("YTM_LIKED_LIMIT", 500)

REFRESH_HOURS = _env_float("REFRESH_HOURS", 12)
LOOKUP_DELAY = _env_float("LOOKUP_DELAY", 1.0)
MAX_ARTISTS = _env_int("MAX_ARTISTS", 0)          # 0 = sin tope
STRICT_MATCH = _env_bool("STRICT_MATCH", True)     # solo coincidencia exacta
EXCLUDE_EXISTING = _env_bool("EXCLUDE_EXISTING", False)
NEGATIVE_TTL_DAYS = _env_int("NEGATIVE_TTL_DAYS", 14)
MAX_COLLAB_PARTS = _env_int("MAX_COLLAB_PARTS", 4)
PORT = _env_int("PORT", 8080)

ARTISTS_FILE = DATA_DIR / "artists.json"
CACHE_FILE = DATA_DIR / "cache.json"
STATE_FILE = DATA_DIR / "state.json"
# Se prueban en orden. 'browser.json' es el nombre por defecto que escribe
# `ytmusicapi browser`, así que se acepta tal cual sin renombrar nada.
AUTH_CANDIDATES = [
    DATA_DIR / "headers_auth.json",
    DATA_DIR / "browser.json",
    DATA_DIR / "oauth.json",
]

# "Various Artists" y "[unknown]" de MusicBrainz: agujeros negros que se
# tragarían media biblioteca si Lidarr llegara a monitorizarlos.
MBID_DENYLIST = {
    "89ad4ac3-39f7-470e-963a-56509c546377",
    "125ec42a-7229-4250-afc5-e057484327fe",
}
NAME_DENYLIST = {"various artists", "unknown artist", "va", "traditional"}

# Subir esto reintenta los negativos cacheados sin tirar los positivos,
# que son los caros de reconstruir.
CACHE_VERSION = 2


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


# ---------------------------------------------------------------
# Normalización de nombres
# ---------------------------------------------------------------
def clean_name(name: str) -> str:
    """Limpia lo que YTM añade a los nombres de canal."""
    if not name:
        return ""
    name = re.sub(r"\s*-\s*Topic$", "", name.strip())
    name = re.sub(r"\s+", " ", name)
    return name.strip()


def normalize(name: str) -> str:
    """Clave de comparación: sin acentos, sin puntuación, en minúsculas.

    Hace que 'Café Tacvba' y 'Cafe Tacvba' colisionen, que es justo lo que
    queremos al comparar contra lo que devuelve Lidarr.
    """
    decomposed = unicodedata.normalize("NFKD", name.casefold())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", stripped)


# ---------------------------------------------------------------
# Estado en disco
# ---------------------------------------------------------------
def load_json(path: Path, default):
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: Path, payload) -> None:
    """Escritura atómica: Lidarr puede estar leyendo artists.json ahora mismo."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    tmp.replace(path)


# ---------------------------------------------------------------
# Recolección desde YouTube Music
# ---------------------------------------------------------------
def _names_from_item(item: dict) -> list[str]:
    """Extrae nombres de artista de un item de YTM (canción, álbum o ficha)."""
    names = []
    for artist in item.get("artists") or []:
        if isinstance(artist, dict) and artist.get("name"):
            names.append(artist["name"])
    # Ficha de artista: tiene 'subscribers' y el nombre vive en 'title'.
    if not names and item.get("subscribers") and item.get("title"):
        names.append(item["title"])
    return names


def collect_from_ytm() -> tuple[list[str], list[str]]:
    """Devuelve (nombres, avisos). Nunca lanza: sin YTM seguimos con Takeout."""
    warnings: list[str] = []
    names: list[str] = []

    auth = next((str(p) for p in AUTH_CANDIDATES if p.exists()), None)
    if auth is None:
        names_tried = ", ".join(p.name for p in AUTH_CANDIDATES)
        return [], [f"sin credenciales de YTM en {DATA_DIR}: se buscó {names_tried}"]

    try:
        from ytmusicapi import YTMusic
        yt = YTMusic(auth)
    except Exception as exc:  # noqa: BLE001 - queremos degradar, no morir
        return [], [f"no se pudo inicializar ytmusicapi: {exc}"]

    if "home" in SOURCES:
        try:
            for row in yt.get_home(limit=HOME_ROWS):
                for item in row.get("contents") or []:
                    names.extend(_names_from_item(item))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"home: {exc}")

    if "library" in SOURCES:
        try:
            for a in yt.get_library_artists(limit=LIBRARY_LIMIT):
                if a.get("artist"):
                    names.append(a["artist"])
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"library: {exc}")

    if "subscriptions" in SOURCES:
        try:
            for a in yt.get_library_subscriptions(limit=LIBRARY_LIMIT):
                if a.get("artist"):
                    names.append(a["artist"])
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"subscriptions: {exc}")

    if "liked" in SOURCES:
        try:
            for track in yt.get_liked_songs(limit=LIKED_LIMIT).get("tracks") or []:
                names.extend(_names_from_item(track))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"liked: {exc}")

    if "history" in SOURCES:
        try:
            for track in yt.get_history():
                names.extend(_names_from_item(track))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"history: {exc}")

    return names, warnings


def collect_from_takeout() -> tuple[list[str], list[str]]:
    """Lee cualquier CSV de Takeout y saca la columna que hable de artistas.

    El formato exacto de Takeout ha cambiado varias veces, así que en vez de
    fijar nombres de columna buscamos cualquier cabecera que contenga
    'artist' o 'artista'.
    """
    warnings: list[str] = []
    names: list[str] = []
    if not TAKEOUT_DIR.is_dir():
        return [], []

    for path in sorted(TAKEOUT_DIR.rglob("*.csv")):
        try:
            with path.open(encoding="utf-8-sig", newline="") as fh:
                reader = csv.DictReader(fh)
                cols = [c for c in (reader.fieldnames or [])
                        if c and ("artist" in c.lower() or "artista" in c.lower())]
                if not cols:
                    continue
                for row in reader:
                    for col in cols:
                        value = (row.get(col) or "").strip()
                        if value:
                            # Takeout separa colaboraciones con ';' o ','.
                            names.extend(re.split(r"\s*[;,]\s*", value))
        except (OSError, csv.Error, UnicodeDecodeError) as exc:
            warnings.append(f"takeout {path.name}: {exc}")

    return names, warnings


# ---------------------------------------------------------------
# Resolución contra Lidarr
# ---------------------------------------------------------------
class Lidarr:
    def __init__(self, url: str, api_key: str):
        self.url = url
        self.session = requests.Session()
        self.session.headers["X-Api-Key"] = api_key

    def lookup(self, term: str) -> list[dict]:
        resp = self.session.get(
            f"{self.url}/api/v1/artist/lookup?term={quote(term)}", timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

    def existing_mbids(self) -> set[str]:
        resp = self.session.get(f"{self.url}/api/v1/artist", timeout=60)
        resp.raise_for_status()
        return {a["foreignArtistId"] for a in resp.json() if a.get("foreignArtistId")}


def split_collaboration(name: str) -> list[str]:
    """Parte 'A, B & C' en artistas sueltos.

    Solo se usa cuando el nombre completo NO resolvió, así que un grupo que
    lleve comas o '&' en su nombre real ya se resolvió antes y nunca llega
    aquí. Ojo con no partir por ' y ': 'El Haragán y Compañía' es un solo
    grupo, y en español ese caso es la norma, no la excepción.
    """
    parts = re.split(r"\s*,\s*|\s*&\s*|\s+and\s+", name)
    clean = [p for p in (x.strip(" .") for x in parts) if len(p) > 2]
    # Un reparto de musical entero son 10 búsquedas por un artista que
    # probablemente no quieres en la biblioteca. Cortamos por lo sano.
    return clean if len(clean) <= MAX_COLLAB_PARTS else []


def pick_match(name: str, results: list[dict]) -> str | None:
    """Elige el MBID correcto de lo que devuelve Lidarr.

    Lidarr ordena por relevancia difusa, y esa primera posición miente a
    menudo: buscando 'Caifanes' devuelve también 'Trío Los Caimanes'. Por eso
    STRICT_MATCH exige coincidencia exacta del nombre normalizado.
    """
    target = normalize(name)
    for r in results:
        mbid = r.get("foreignArtistId")
        if mbid and mbid not in MBID_DENYLIST and normalize(r.get("artistName", "")) == target:
            return mbid
    if STRICT_MATCH:
        return None
    for r in results:
        mbid = r.get("foreignArtistId")
        if mbid and mbid not in MBID_DENYLIST:
            return mbid
    return None


# ---------------------------------------------------------------
# Refresco
# ---------------------------------------------------------------
_refresh_lock = threading.Lock()


def refresh() -> dict:
    """Reconstruye artists.json. Devuelve el resumen que sirve /status."""
    if not _refresh_lock.acquire(blocking=False):
        return {**load_json(STATE_FILE, {}), "note": "ya había un refresco en curso"}

    started = time.monotonic()
    try:
        log("refresco: recolectando artistas")
        ytm_names, warnings = collect_from_ytm()
        takeout_names, takeout_warnings = collect_from_takeout()
        warnings += takeout_warnings

        # Deduplicado conservando el primer nombre "bonito" de cada artista.
        seen: dict[str, str] = {}
        for raw in ytm_names + takeout_names:
            name = clean_name(raw)
            key = normalize(name)
            if not key or key in seen or name.casefold() in NAME_DENYLIST:
                continue
            seen[key] = name

        log(f"refresco: {len(seen)} artistas únicos "
            f"({len(ytm_names)} de YTM, {len(takeout_names)} de Takeout)")

        cache = load_json(CACHE_FILE, {})
        lidarr = Lidarr(LIDARR_URL, LIDARR_API_KEY)
        now = time.time()
        negative_ttl = NEGATIVE_TTL_DAYS * 86400

        mbids: list[str] = []
        unresolved: list[str] = []
        looked_up = 0

        for key, name in seen.items():
            entry = cache.get(key)
            if entry is not None:
                found = entry.get("mbids") or ([entry["mbid"]] if entry.get("mbid") else [])
                # Los positivos valen siempre; los negativos caducan y se
                # reintentan también si los resolvió una versión anterior.
                fresh = found or (
                    (now - entry.get("ts", 0)) < negative_ttl
                    and entry.get("v", 1) >= CACHE_VERSION
                )
                if fresh:
                    if found:
                        mbids.extend(found)
                    else:
                        unresolved.append(name)
                    continue

            # Cache miss (o negativo caducado): preguntamos a Lidarr.
            try:
                found = []
                mbid = pick_match(name, lidarr.lookup(name))
                looked_up += 1
                if mbid:
                    found = [mbid]
                else:
                    # 'Bethel Music, Brian Johnson & Jenn Johnson' es tres
                    # artistas, no uno. Solo se intenta si el nombre entero
                    # ya falló, así que no rompe nombres con comas propias.
                    parts = split_collaboration(name)
                    if len(parts) > 1:
                        for part in parts:
                            time.sleep(LOOKUP_DELAY)
                            part_mbid = pick_match(part, lidarr.lookup(part))
                            looked_up += 1
                            if part_mbid:
                                found.append(part_mbid)
            except requests.RequestException as exc:
                # Sin cachear: un 503 pasajero de Lidarr no debe fijar un
                # negativo durante dos semanas.
                warnings.append(f"lookup '{name}': {exc}")
                continue

            cache[key] = {"mbids": found, "name": name, "ts": now, "v": CACHE_VERSION}
            if found:
                mbids.extend(found)
            else:
                unresolved.append(name)
            time.sleep(LOOKUP_DELAY)

        if looked_up:
            save_json(CACHE_FILE, cache)

        # Deduplicado final: varios nombres pueden mapear al mismo MBID.
        mbids = list(dict.fromkeys(mbids))

        if EXCLUDE_EXISTING:
            try:
                have = lidarr.existing_mbids()
                before = len(mbids)
                mbids = [m for m in mbids if m not in have]
                log(f"refresco: {before - len(mbids)} ya estaban en Lidarr, excluidos")
            except requests.RequestException as exc:
                warnings.append(f"no se pudo listar la biblioteca de Lidarr: {exc}")

        if MAX_ARTISTS > 0:
            mbids = mbids[:MAX_ARTISTS]

        save_json(ARTISTS_FILE, [{"MusicBrainzId": m} for m in mbids])

        state = {
            "lastRefresh": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "durationSeconds": round(time.monotonic() - started, 1),
            "sources": SOURCES,
            "namesCollected": len(seen),
            "artistsPublished": len(mbids),
            "lookupsPerformed": looked_up,
            "unresolvedCount": len(unresolved),
            "unresolvedSample": sorted(unresolved)[:50],
            "warnings": warnings,
        }
        save_json(STATE_FILE, state)
        log(f"refresco: publicados {len(mbids)} artistas, "
            f"{len(unresolved)} sin resolver, {len(warnings)} avisos")
        return state
    finally:
        _refresh_lock.release()


def refresh_loop() -> None:
    while True:
        try:
            refresh()
        except Exception as exc:  # noqa: BLE001 - el hilo no puede morir
            log(f"refresco falló: {exc}")
        time.sleep(max(REFRESH_HOURS, 0.25) * 3600)


# ---------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------
app = Flask(__name__)


@app.get("/artists.json")
def artists():
    # Si aún no hay refresco, una lista vacía es válida para Lidarr.
    if not ARTISTS_FILE.exists():
        return jsonify([])
    return send_file(ARTISTS_FILE, mimetype="application/json")


@app.get("/status")
def status():
    state = load_json(STATE_FILE, {})
    state["config"] = {
        "lidarrUrl": LIDARR_URL,
        "sources": SOURCES,
        "refreshHours": REFRESH_HOURS,
        "strictMatch": STRICT_MATCH,
        "excludeExisting": EXCLUDE_EXISTING,
        "ytmAuth": next((p.name for p in AUTH_CANDIDATES if p.exists()), None),
        "takeoutDir": str(TAKEOUT_DIR) if TAKEOUT_DIR.is_dir() else None,
    }
    return jsonify(state)


@app.get("/health")
def health():
    return jsonify({"ok": True, "artists": len(load_json(ARTISTS_FILE, []))})


@app.post("/refresh")
def manual_refresh():
    return jsonify(refresh())


def main() -> None:
    if not LIDARR_API_KEY:
        raise SystemExit("falta LIDARR_API_KEY")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not any(p.exists() for p in AUTH_CANDIDATES):
        log(f"AVISO: no hay credenciales de YTM en {DATA_DIR} "
            f"({', '.join(p.name for p in AUTH_CANDIDATES)}); "
            "solo se usará Takeout hasta que las pongas")

    threading.Thread(target=refresh_loop, daemon=True).start()
    log(f"escuchando en :{PORT} -> Custom List de Lidarr: /artists.json")
    waitress_serve(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()

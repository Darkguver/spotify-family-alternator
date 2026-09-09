"""
Spotify Playlist Alternator
----------------------------
Kleine webservice die twee Spotify playlists (bv. "kinderen" en "volwassenen")
om-en-om naar een actief Spotify Connect apparaat (bv. de Tesla) stuurt, zodat
iedereen om de beurt zijn eigen muziek hoort.

Draai lokaal met:  uvicorn main:app --reload
"""
import json
import os
import random
import secrets
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from spotipy import Spotify, SpotifyException
from spotipy.oauth2 import SpotifyOAuth

load_dotenv()

BASE_DIR = Path(__file__).parent
CACHE_DIR = BASE_DIR / ".spotify_caches"
CACHE_DIR.mkdir(exist_ok=True)
SETTINGS_FILE = BASE_DIR / "settings.json"

SCOPE = "user-read-playback-state user-modify-playback-state playlist-read-private playlist-read-collaborative"

CLIENT_ID = os.environ["SPOTIFY_CLIENT_ID"]
CLIENT_SECRET = os.environ["SPOTIFY_CLIENT_SECRET"]
REDIRECT_URI = os.environ["SPOTIFY_REDIRECT_URI"]
SESSION_SECRET = os.environ.get("SESSION_SECRET", secrets.token_hex(16))

app = FastAPI(title="Spotify Playlist Alternator")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

# ---------------------------------------------------------------------------
# Gedeelde, mobiel-vriendelijke styling (dark theme, grote touch-targets,
# safe-area support voor iPhone notch/home-indicator).
# ---------------------------------------------------------------------------
MOBILE_CSS = """
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body {
    margin: 0; padding: 0;
    background: #121212; color: #f2f2f2;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  body {
    padding: env(safe-area-inset-top, 16px) env(safe-area-inset-right, 16px)
             env(safe-area-inset-bottom, 24px) env(safe-area-inset-left, 16px);
  }
  .card {
    max-width: 480px; margin: 0 auto;
  }
  h1 { font-size: 1.6em; margin: 8px 0 4px; }
  h2 { font-size: 1.05em; margin: 24px 0 8px; color: #b3b3b3; }
  a { color: #1DB954; }
  p.muted { color: #b3b3b3; margin: 4px 0 16px; }
  p.small { font-size: 0.85em; }
  label {
    display: block; margin: 14px 0 6px; font-size: 0.95em; color: #d9d9d9;
  }
  label.checkbox { display: flex; align-items: center; gap: 8px; }
  label.checkbox input { width: auto; }
  input[type="text"], input:not([type]), input[type="number"], select {
    width: 100%; padding: 12px 14px; font-size: 16px;
    border-radius: 10px; border: 1px solid #333; background: #1e1e1e; color: #f2f2f2;
  }
  input[type="checkbox"] {
    width: 22px; height: 22px;
  }
  .row { display: flex; gap: 12px; }
  .row-item { flex: 1; }
  .playlist-row {
    display: flex; align-items: center; gap: 12px;
    padding: 10px 0; border-bottom: 1px solid #262626;
  }
  .playlist-label { flex: 1; margin: 0; display: flex; align-items: center; gap: 10px; }
  .playlist-label input[type="checkbox"] { flex: 0 0 auto; }
  .count-label { margin: 0; width: 84px; flex: 0 0 auto; font-size: 0.8em; }
  .count-label input { padding: 8px 10px; }
  button, .btn {
    display: block; width: 100%; text-align: center;
    padding: 14px 20px; margin-top: 14px; font-size: 1.05em; font-weight: 600;
    border-radius: 24px; border: none; text-decoration: none;
    -webkit-appearance: none; appearance: none;
  }
  .btn-primary { background: #1DB954; color: #fff; }
  .btn-secondary { background: #2a2a2a; color: #f2f2f2; }
  .btn-outline { background: transparent; color: #f2f2f2; border: 1px solid #444; }
  form { margin-bottom: 8px; }
  select { -webkit-appearance: none; appearance: none; }
</style>
"""


def page(body: str) -> str:
    """Wikkelt losse HTML-snippets in dezelfde mobiel-vriendelijke pagina-shell."""
    return f"""
    <html>
    <head>
      <title>Spotify Alternator</title>
      <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
      <meta name="apple-mobile-web-app-capable" content="yes">
      <meta name="theme-color" content="#121212">
      {MOBILE_CSS}
    </head>
    <body><div class="card">{body}</div></body></html>
    """


# ---------------------------------------------------------------------------
# Instellingen (geselecteerde playlists + aantal nummers per beurt) persistent
# in settings.json
# ---------------------------------------------------------------------------
DEFAULT_SETTINGS = {
    "playlists": [],  # lijst van {"id": ..., "name": ..., "count": 1}
    "shuffle": True,
}


def load_settings() -> dict:
    data = json.loads(SETTINGS_FILE.read_text()) if SETTINGS_FILE.exists() else {}
    settings = {**DEFAULT_SETTINGS, **data}
    # Migratie vanaf de oude vaste kinderen/volwassenen-instelling, zodat
    # bestaande configuraties niet verloren gaan.
    if not settings.get("playlists") and (data.get("kids_playlist") or data.get("adults_playlist")):
        migrated = []
        if data.get("kids_playlist"):
            migrated.append({
                "id": data["kids_playlist"],
                "name": "Kinderen (oude instelling)",
                "count": data.get("kids_count", 1),
            })
        if data.get("adults_playlist"):
            migrated.append({
                "id": data["adults_playlist"],
                "name": "Volwassenen (oude instelling)",
                "count": data.get("adults_count", 1),
            })
        settings["playlists"] = migrated
    return settings


def save_settings(settings: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2))


# ---------------------------------------------------------------------------
# Spotify OAuth helpers
# ---------------------------------------------------------------------------
def get_oauth(session_id: str) -> SpotifyOAuth:
    return SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope=SCOPE,
        cache_path=str(CACHE_DIR / f".cache-{session_id}"),
        show_dialog=False,
    )


def get_session_id(request: Request) -> str:
    sid = request.session.get("sid")
    if not sid:
        sid = secrets.token_hex(16)
        request.session["sid"] = sid
    return sid


def get_spotify(request: Request) -> Spotify | None:
    sid = get_session_id(request)
    oauth = get_oauth(sid)
    token_info = oauth.get_cached_token()
    if not token_info:
        return None
    if oauth.is_token_expired(token_info):
        token_info = oauth.refresh_access_token(token_info["refresh_token"])
    return Spotify(auth=token_info["access_token"])


# ---------------------------------------------------------------------------
# Playlist helpers
# ---------------------------------------------------------------------------
def fetch_user_playlists(sp: Spotify) -> list[dict]:
    """Haalt alle playlists op die in de bibliotheek van de ingelogde gebruiker
    staan (eigen playlists + gevolgde playlists van anderen)."""
    playlists = []
    url = "https://api.spotify.com/v1/me/playlists"
    params = {"limit": 50}
    headers = {"Authorization": f"Bearer {sp._auth}"}
    while url:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        if resp.status_code != 200:
            raise SpotifyException(resp.status_code, -1, f"{url} {resp.text}")
        data = resp.json()
        for item in data.get("items", []):
            if not item:
                continue
            owner = item.get("owner") or {}
            playlists.append({
                "id": item.get("id"),
                "name": item.get("name") or "(zonder naam)",
                "owner": owner.get("display_name") or owner.get("id") or "",
                "owner_id": owner.get("id"),
                "tracks_total": (item.get("tracks") or {}).get("total"),
            })
        url = data.get("next")
        params = None
    return playlists


def fetch_playlist_track_uris(sp: Spotify, playlist_id: str) -> list[str]:
    """Haalt track-URI's op via de nieuwere /items endpoint.

    Spotify's legacy /playlists/{id}/tracks endpoint (gebruikt door
    spotipy's playlist_items()) geeft sinds enkele weken een kale 403
    Forbidden voor apps in Development Mode, zelfs voor eigen playlists.
    De nieuwere /items endpoint werkt wel met dezelfde token/scopes, dus
    die roepen we hier rechtstreeks aan.
    """
    uris = []
    if not playlist_id:
        return uris
    url = f"https://api.spotify.com/v1/playlists/{playlist_id}/items"
    params = {
        "limit": 100,
        "offset": 0,
        "fields": "items.item.uri,items.item.is_local,next",
        "additional_types": "track",
    }
    headers = {"Authorization": f"Bearer {sp._auth}"}
    while url:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        if resp.status_code != 200:
            raise SpotifyException(resp.status_code, -1, f"{url} {resp.text}")
        data = resp.json()
        for entry in data.get("items", []):
            item = entry.get("item")
            if item and item.get("uri"):
                uris.append(item["uri"])
        url = data.get("next")
        params = None
    return uris


def interleave_multi(track_lists: list[list[str]], counts: list[int]) -> list[str]:
    """Rouleert om-en-om door een willekeurig aantal playlists, waarbij van
    elke playlist steeds `count` nummers achter elkaar worden gepakt voordat
    naar de volgende playlist wordt gegaan."""
    out = []
    indices = [0] * len(track_lists)
    progress = True
    while progress:
        progress = False
        for idx, (tracks, count) in enumerate(zip(track_lists, counts)):
            taken = 0
            limit = max(count, 0)
            while taken < limit and indices[idx] < len(tracks):
                out.append(tracks[indices[idx]])
                indices[idx] += 1
                taken += 1
                progress = True
    return out


# ---------------------------------------------------------------------------
# Routes
@app.get("/login")
def login(request: Request):
    sid = get_session_id(request)
    oauth = get_oauth(sid)
    return RedirectResponse(oauth.get_authorize_url())


@app.get("/callback")
def callback(request: Request, code: str = "", error: str = ""):
    if error or not code:
        reason = error or "geen autorisatiecode ontvangen"
        return HTMLResponse(page(f"<p>Spotify login mislukt: {reason}. <a href='/login'>Opnieuw proberen</a></p>"))
    sid = get_session_id(request)
    oauth = get_oauth(sid)
    oauth.get_access_token(code, as_dict=True)
    return RedirectResponse("/")


@app.get("/logout")
def logout(request: Request):
    sid = request.session.get("sid")
    if sid:
        cache_file = CACHE_DIR / f".cache-{sid}"
        cache_file.unlink(missing_ok=True)
    request.session.clear()
    return RedirectResponse("/")


@app.post("/settings")
async def update_settings(request: Request):
    form = await request.form()
    settings = load_settings()
    selected_ids = form.getlist("playlist_id")
    playlists = []
    for pid in selected_ids:
        name = str(form.get(f"name_{pid}") or pid)
        try:
            count = int(form.get(f"count_{pid}") or 1)
        except ValueError:
            count = 1
        playlists.append({"id": pid, "name": name, "count": count})
    settings["playlists"] = playlists
    settings["shuffle"] = form.get("shuffle") == "on"
    save_settings(settings)
    return RedirectResponse("/", status_code=303)


@app.post("/start")
async def start_playback(request: Request):
    sp = get_spotify(request)
    if not sp:
        return RedirectResponse("/login")
    form = await request.form()
    device_id = str(form.get("device_id") or "")
    settings = load_settings()
    selected = settings.get("playlists", [])

    if not selected:
        return HTMLResponse(page(
            "<p>Er zijn nog geen playlists geselecteerd. Ga terug, vink minstens één "
            "playlist aan en sla de instellingen op.</p><p><a href='/'>Terug</a></p>"
        ))

    try:
        my_id = sp.current_user().get("id")
    except SpotifyException:
        my_id = None
    try:
        owned_by = {p["id"]: p.get("owner") for p in fetch_user_playlists(sp)}
    except Exception:
        owned_by = {}

    track_lists, counts, names, errors = [], [], [], []
    for entry in selected:
        pid = entry.get("id")
        name = entry.get("name") or pid
        count = entry.get("count", 1)
        try:
            uris = fetch_playlist_track_uris(sp, pid)
        except Exception as e:
            uris = []
            note = ""
            if owned_by.get(pid) and my_id and owned_by[pid] != my_id:
                note = (
                    " ⚠️ Deze playlist is niet van jouw eigen account. Spotify's Development Mode "
                    "blokkeert soms het ophalen van tracks uit playlists van andere accounts. "
                    "Dupliceer de playlist naar je eigen account ('···' → Dupliceren) en selecteer die kopie."
                )
            errors.append(f"{name}: fout bij ophalen ({e}).{note}")
        if settings.get("shuffle", True):
            random.shuffle(uris)
        track_lists.append(uris)
        counts.append(count)
        names.append(name)

    if errors:
        details = "".join(f"<li>{e}</li>" for e in errors)
        return HTMLResponse(page(
            f"<p>Er ging iets mis bij het ophalen van (een van) de playlists:</p><ul>{details}</ul>"
            "<p><a href='/'>Terug</a></p>"
        ))

    mix = interleave_multi(track_lists, counts)
    mix = mix[:500]  # veiligheidslimiet

    if not mix:
        details = "".join(
            f"<li>{n}: 0 nummers gevonden.</li>" for n in names
        )
        return HTMLResponse(page(
            f"<p>Geen nummers gevonden. Details:</p><ul>{details}</ul><p><a href='/'>Terug</a></p>"
        ))

    sp.start_playback(device_id=device_id or None, uris=mix)
    return RedirectResponse(
        f"/?started=1&mix_n={len(mix)}&playlists_n={len(selected)}",
        status_code=303,
    )


@app.post("/stop")
async def stop_playback(request: Request):
    sp = get_spotify(request)
    if sp:
        try:
            sp.pause_playback()
        except Exception:
            pass
    return RedirectResponse("/", status_code=303)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    sp = get_spotify(request)
    settings = load_settings()

    if not sp:
        return HTMLResponse(f"""
        <html>
        <head>
          <title>Spotify Alternator</title>
          <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
          <meta name="apple-mobile-web-app-capable" content="yes">
          <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
          <meta name="theme-color" content="#121212">
          {MOBILE_CSS}
        </head>
        <body>
          <div class="card">
            <h1>🎵 Spotify Alternator</h1>
            <p>Log eerst in met het Spotify-account dat ook in de Tesla is ingelogd.</p>
            <a class="btn btn-primary" href="/login">Inloggen met Spotify</a>
          </div>
        </body></html>
        """)

    profile = sp.current_user()
    devices = sp.devices().get("devices", [])
    device_options = "".join(
        f'<option value="{d["id"]}" {"selected" if d.get("is_active") else ""}>'
        f'{d["name"]} ({d["type"]})</option>'
        for d in devices
    ) or "<option value=''>Geen apparaten gevonden — open Spotify in de Tesla</option>"

    started_note = ""
    if request.query_params.get("started") == "1":
        mix_n = request.query_params.get("mix_n", "?")
        playlists_n = request.query_params.get("playlists_n", "?")
        started_note = (
            f"<p style='color:#1DB954'>▶️ Gestart: {playlists_n} playlists geroteerd, "
            f"{mix_n} nummers in de afspeel-queue gezet.</p>"
        )

    selected_map = {p["id"]: p for p in settings.get("playlists", [])}
    playlists_error = ""
    try:
        my_id = sp.current_user().get("id")
    except SpotifyException:
        my_id = None
    try:
        all_playlists = fetch_user_playlists(sp)
    except Exception as e:
        all_playlists = []
        playlists_error = f"<p class='muted'>⚠️ Kon je playlists niet ophalen: {e}</p>"

    # Alleen playlists van het eigen account tonen: Spotify's Development Mode
    # blokkeert het ophalen van tracks uit playlists van andere accounts, dus
    # playlists van anderen zouden hier toch altijd een 403 geven.
    my_playlists = [p for p in all_playlists if p.get("owner_id") == my_id]
    others_count = len(all_playlists) - len(my_playlists)

    rows = ""
    for p in my_playlists:
        pid = p["id"]
        checked = "checked" if pid in selected_map else ""
        count_val = selected_map.get(pid, {}).get("count", 1)
        subtitle_bits = [b for b in [p.get("owner"), (f"{p['tracks_total']} nummers" if p.get("tracks_total") is not None else None)] if b]
        subtitle = " · ".join(subtitle_bits)
        rows += f"""
        <div class="playlist-row">
          <label class="checkbox playlist-label">
            <input type="checkbox" name="playlist_id" value="{pid}" {checked}>
            <span><b>{p['name']}</b><br><span class="muted small">{subtitle}</span></span>
          </label>
          <input type="hidden" name="name_{pid}" value="{p['name']}">
          <label class="row-item count-label">Nummers op rij
            <input type="number" min="0" inputmode="numeric" name="count_{pid}" value="{count_val}"></label>
        </div>
        """

    missing = [sp_pl for sp_pl in settings.get("playlists", []) if sp_pl["id"] not in {p["id"] for p in my_playlists}]
    missing_note = ""
    if missing:
        missing_names = ", ".join(m["name"] for m in missing)
        missing_note = (
            f"<p class='muted small'>⚠️ Eerder geselecteerd maar niet meer bruikbaar: "
            f"{missing_names}. Ze doen niet meer mee totdat je ze opnieuw selecteert.</p>"
        )

    others_note = ""
    if others_count:
        others_note = (
            f"<p class='muted small'>{others_count} playlist(s) van andere accounts in je bibliotheek zijn "
            "verborgen — Spotify staat het ophalen van tracks daaruit niet toe. Dupliceer zo'n playlist naar "
            "je eigen account ('···' → Dupliceren) om 'm hier te kunnen gebruiken.</p>"
        )

    if not my_playlists:
        rows = "<p class='muted'>Geen eigen playlists gevonden in je Spotify-bibliotheek.</p>"

    return HTMLResponse(f"""
    <html>
    <head>
      <title>Spotify Alternator</title>
      <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
      <meta name="apple-mobile-web-app-capable" content="yes">
      <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
      <meta name="theme-color" content="#121212">
      {MOBILE_CSS}
    </head>
    <body>
      <div class="card">
        <h1>🎵 Spotify Alternator</h1>
        <p class="muted">Ingelogd als <b>{profile.get("display_name")}</b> — <a href="/logout">uitloggen</a></p>
        {started_note}

        <h2>1. Kies je playlists</h2>
        <p class="muted small">Vink de playlists aan die mee moeten draaien in de rotatie (bv. jouw playlist,
        die van je vrouw, en die van je dochter) en geef aan hoeveel nummers er per beurt achter elkaar
        van die playlist gespeeld worden.</p>
        {playlists_error}
        {missing_note}
        {others_note}
        <form method="post" action="/settings">
          {rows}
          <label class="checkbox"><input type="checkbox" name="shuffle" {"checked" if settings.get("shuffle", True) else ""}>
            Shuffle binnen elke playlist</label>
          <button type="submit" class="btn btn-secondary">Instellingen opslaan</button>
        </form>

        <h2>2. Afspelen</h2>
        <form method="post" action="/start">
          <label>Apparaat (kies de Tesla / auto)
            <select name="device_id">{device_options}</select></label>
          <button type="submit" class="btn btn-primary">▶️ Start om-en-om afspelen</button>
        </form>
        <form method="post" action="/stop">
          <button type="submit" class="btn btn-outline">⏸️ Pauzeren</button>
        </form>

        <p class="muted small">Tip: open eerst de Spotify-app in de Tesla (of laat 'm actief spelen)
        zodat hij hierboven in de apparatenlijst verschijnt.</p>
      </div>
    </body></html>
    """)

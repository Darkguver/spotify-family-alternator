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
# Instellingen (playlist-ids + afwissel-patroon) persistent in settings.json
# ---------------------------------------------------------------------------
DEFAULT_SETTINGS = {
    "kids_playlist": os.environ.get("KIDS_PLAYLIST_ID", ""),
    "adults_playlist": os.environ.get("ADULTS_PLAYLIST_ID", ""),
    "kids_count": 1,
    "adults_count": 1,
    "shuffle": True,
}


def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        data = json.loads(SETTINGS_FILE.read_text())
        merged = {**DEFAULT_SETTINGS, **data}
        return merged
    return dict(DEFAULT_SETTINGS)


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
def extract_playlist_id(value: str) -> str:
    """Accepteert een kale playlist-id, een spotify: URI of een open.spotify.com URL."""
    value = value.strip()
    if value.startswith("spotify:playlist:"):
        return value.split(":")[-1]
    if "open.spotify.com/playlist/" in value:
        tail = value.split("open.spotify.com/playlist/")[-1]
        return tail.split("?")[0].split("/")[0]
    return value


def fetch_playlist_info(sp: Spotify, playlist_id: str) -> dict:
    """Haalt naam/eigenaar op, zodat we duidelijke foutmeldingen kunnen geven."""
    if not playlist_id:
        return {"name": None, "owner": None, "error": None}
    try:
        meta = sp.playlist(playlist_id, fields="name,owner.id,owner.display_name")
        return {
            "name": meta.get("name"),
            "owner": (meta.get("owner") or {}).get("id"),
            "owner_name": (meta.get("owner") or {}).get("display_name"),
            "error": None,
        }
    except SpotifyException as e:
        return {"name": None, "owner": None, "error": str(e)}


def fetch_playlist_track_uris(sp: Spotify, playlist_id: str) -> list[str]:
    uris = []
    if not playlist_id:
        return uris
    results = sp.playlist_items(
        playlist_id,
        fields="items.track.uri,items.track.is_local,next",
        additional_types=["track"],
    )
    while results:
        for item in results["items"]:
            track = item.get("track")
            if track and track.get("uri"):
                uris.append(track["uri"])
        results = sp.next(results) if results.get("next") else None
    return uris


def interleave(kids: list[str], adults: list[str], kids_count: int, adults_count: int) -> list[str]:
    out = []
    i, j = 0, 0
    while i < len(kids) or j < len(adults):
        for _ in range(max(kids_count, 0)):
            if i < len(kids):
                out.append(kids[i])
                i += 1
        for _ in range(max(adults_count, 0)):
            if j < len(adults):
                out.append(adults[j])
                j += 1
        if kids_count <= 0 and adults_count <= 0:
            break
    return out


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/debug")
def debug(request: Request):
    """Tijdelijke diagnosepagina om 403-fouten van Spotify te doorgronden."""
    sid = get_session_id(request)
    oauth = get_oauth(sid)
    token_info = oauth.get_cached_token()
    if not token_info:
        return HTMLResponse("<p>Niet ingelogd. <a href='/login'>Log eerst in</a>.</p>")
    if oauth.is_token_expired(token_info):
        token_info = oauth.refresh_access_token(token_info["refresh_token"])

    import requests as _requests
    access_token = token_info["access_token"]
    headers = {"Authorization": f"Bearer {access_token}"}

    out = [f"<p><b>Scope van token:</b> {token_info.get('scope')}</p>"]

    me = _requests.get("https://api.spotify.com/v1/me", headers=headers)
    out.append(f"<p><b>/me:</b> {me.status_code} — {me.text[:500]}</p>")

    settings = load_settings()
    for label, pid in [("kids", settings.get("kids_playlist")), ("adults", settings.get("adults_playlist"))]:
        if not pid:
            continue
        meta = _requests.get(f"https://api.spotify.com/v1/playlists/{pid}", headers=headers)
        out.append(f"<p><b>{label} playlist meta ({pid}):</b> {meta.status_code} — {meta.text[:800]}</p>")
        tracks = _requests.get(f"https://api.spotify.com/v1/playlists/{pid}/tracks", headers=headers, params={"limit": 5})
        out.append(f"<p><b>{label} playlist tracks:</b> {tracks.status_code} — {tracks.text[:800]}</p>")

    return HTMLResponse("<html><body style='font-family:sans-serif;max-width:800px;margin:40px auto;word-wrap:break-word;'>"
                         + "".join(out) + "<p><a href='/'>Terug</a></p></body></html>")


@app.get("/login")
def login(request: Request):
    sid = get_session_id(request)
    oauth = get_oauth(sid)
    return RedirectResponse(oauth.get_authorize_url())


@app.get("/callback")
def callback(request: Request, code: str = "", error: str = ""):
    if error or not code:
        reason = error or "geen autorisatiecode ontvangen"
        return HTMLResponse(f"<p>Spotify login mislukt: {reason}. <a href='/login'>Opnieuw proberen</a></p>")
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
    settings["kids_playlist"] = extract_playlist_id(str(form.get("kids_playlist", "")))
    settings["adults_playlist"] = extract_playlist_id(str(form.get("adults_playlist", "")))
    settings["kids_count"] = int(form.get("kids_count") or 1)
    settings["adults_count"] = int(form.get("adults_count") or 1)
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

    try:
        kids = fetch_playlist_track_uris(sp, settings["kids_playlist"])
    except SpotifyException as e:
        return HTMLResponse(f"<p>Kon de playlist voor kinderen niet ophalen: {e}. <a href='/'>Terug</a></p>")
    try:
        adults = fetch_playlist_track_uris(sp, settings["adults_playlist"])
    except SpotifyException as e:
        return HTMLResponse(f"<p>Kon de playlist voor volwassenen niet ophalen: {e}. <a href='/'>Terug</a></p>")

    if settings.get("shuffle", True):
        random.shuffle(kids)
        random.shuffle(adults)

    mix = interleave(kids, adults, settings["kids_count"], settings["adults_count"])
    mix = mix[:500]  # veiligheidslimiet

    if not mix:
        kids_info = fetch_playlist_info(sp, settings["kids_playlist"])
        adults_info = fetch_playlist_info(sp, settings["adults_playlist"])

        def describe(label: str, playlist_id: str, count: int, info: dict) -> str:
            if not playlist_id:
                return f"<li>{label}: geen playlist ingevuld.</li>"
            if info.get("error"):
                return f"<li>{label}: kon playlist niet vinden ({info['error']}). Controleer de link/ID.</li>"
            owner_note = ""
            if info.get("owner") == "spotify":
                owner_note = (
                    " ⚠️ Dit is een officiële Spotify-playlist (eigenaar: Spotify). "
                    "Spotify staat sinds eind 2024 niet meer toe dat apps zoals deze de nummers "
                    "van hun eigen redactionele/algoritmische playlists ophalen. "
                    "Maak een kopie: open de playlist in Spotify, kies 'Dupliceren' "
                    "(of maak een eigen playlist en sleep de nummers erin), en gebruik die eigen playlist hier."
                )
            return f"<li>{label}: '{info.get('name') or playlist_id}' — {count} nummers gevonden.{owner_note}</li>"

        details = describe("Kinderen", settings["kids_playlist"], len(kids), kids_info)
        details += describe("Volwassenen", settings["adults_playlist"], len(adults), adults_info)
        return HTMLResponse(
            f"<p>Geen nummers gevonden. Details:</p><ul>{details}</ul><p><a href='/'>Terug</a></p>"
        )

    sp.start_playback(device_id=device_id or None, uris=mix)
    return RedirectResponse("/?started=1", status_code=303)


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
        <html><head><title>Spotify Alternator</title></head>
        <body style="font-family:sans-serif;max-width:600px;margin:40px auto;">
          <h1>🎵 Spotify Playlist Alternator</h1>
          <p>Log eerst in met het Spotify-account dat ook in de Tesla is ingelogd.</p>
          <a href="/login" style="background:#1DB954;color:white;padding:10px 20px;border-radius:20px;text-decoration:none;">
            Inloggen met Spotify
          </a>
        </body></html>
        """)

    profile = sp.current_user()
    devices = sp.devices().get("devices", [])
    device_options = "".join(
        f'<option value="{d["id"]}" {"selected" if d.get("is_active") else ""}>'
        f'{d["name"]} ({d["type"]})</option>'
        for d in devices
    ) or "<option value=''>Geen apparaten gevonden — open Spotify in de Tesla</option>"

    return HTMLResponse(f"""
    <html><head><title>Spotify Alternator</title></head>
    <body style="font-family:sans-serif;max-width:600px;margin:40px auto;">
      <h1>🎵 Spotify Playlist Alternator</h1>
      <p>Ingelogd als <b>{profile.get("display_name")}</b> — <a href="/logout">uitloggen</a></p>

      <h2>1. Playlists</h2>
      <form method="post" action="/settings">
        <label>Playlist kinderen (link of ID)<br>
          <input style="width:100%" name="kids_playlist" value="{settings['kids_playlist']}"></label><br><br>
        <label>Playlist volwassenen (link of ID)<br>
          <input style="width:100%" name="adults_playlist" value="{settings['adults_playlist']}"></label><br><br>

        <h2>2. Patroon (hoeveel nummers per beurt)</h2>
        <label>Aantal nummers kinderen op rij:
          <input type="number" min="0" name="kids_count" value="{settings['kids_count']}" style="width:60px"></label><br>
        <label>Aantal nummers volwassenen op rij:
          <input type="number" min="0" name="adults_count" value="{settings['adults_count']}" style="width:60px"></label><br>
        <label><input type="checkbox" name="shuffle" {"checked" if settings.get("shuffle", True) else ""}>
          Shuffle binnen elke playlist</label><br><br>
        <button type="submit">Instellingen opslaan</button>
      </form>

      <h2>3. Afspelen</h2>
      <form method="post" action="/start">
        <label>Apparaat (kies de Tesla / auto)<br>
          <select name="device_id" style="width:100%">{device_options}</select></label><br><br>
        <button type="submit" style="background:#1DB954;color:white;padding:10px 20px;border-radius:20px;border:none;">
          ▶️ Start om-en-om afspelen
        </button>
      </form>
      <form method="post" action="/stop" style="margin-top:10px;">
        <button type="submit">⏸️ Pauzeren</button>
      </form>

      <p style="color:gray;font-size:0.9em;">Tip: open eerst de Spotify-app in de Tesla (of laat 'm actief spelen)
      zodat hij hierboven in de apparatenlijst verschijnt.</p>
    </body></html>
    """)

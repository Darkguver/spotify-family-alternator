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
from spotipy import Spotify
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

    kids = fetch_playlist_track_uris(sp, settings["kids_playlist"])
    adults = fetch_playlist_track_uris(sp, settings["adults_playlist"])
    if settings.get("shuffle", True):
        random.shuffle(kids)
        random.shuffle(adults)

    mix = interleave(kids, adults, settings["kids_count"], settings["adults_count"])
    mix = mix[:500]  # veiligheidslimiet

    if not mix:
        return HTMLResponse("<p>Geen nummers gevonden, controleer de playlist-ids. <a href='/'>Terug</a></p>")

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

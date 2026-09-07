# Spotify Playlist Alternator 🎵

Speelt om-en-om nummers uit **jullie playlist** en de **kinderplaylist** af op
een actief Spotify Connect apparaat (bv. de ingebouwde Spotify-app in de Tesla),
zodat iedereen om de beurt zijn muziek hoort. Het patroon is instelbaar in de
webinterface (bv. 1 op 1, of 2 nummers kind / 1 nummer volwassene).

⚠️ Vereisten: **Spotify Premium** (playback-besturing via de API werkt alleen
met Premium) en het account moet **ook zijn ingelogd in de Tesla**.

## 1. Spotify Developer app aanmaken

1. Ga naar https://developer.spotify.com/dashboard en log in.
2. Klik **Create app**.
   - App name: bv. "Family Car Alternator"
   - Redirect URI: vul (voorlopig) `http://127.0.0.1:8000/callback` in
   - Vink "Web API" aan.
3. Open de app-instellingen (**Settings**) en noteer:
   - **Client ID**
   - **Client Secret** (klik "View client secret")

## 2. Lokaal testen

```bash
cd spotify-alternator
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# vul .env in met je Client ID / Secret
uvicorn main:app --reload
```

Open http://127.0.0.1:8000, log in met Spotify, vul de twee playlists en het
patroon in, kies je auto als apparaat en klik **Start**.

## 3. Deployen naar Render (gratis)

1. Zet deze map in een git-repo en push naar GitHub.
2. Ga naar https://render.com → **New +** → **Web Service** → koppel je repo.
3. Render herkent Python automatisch via `requirements.txt`/`Procfile`.
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Zet onder **Environment** deze variabelen (uit `.env.example`):
   `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_REDIRECT_URI`,
   `SESSION_SECRET`.
   - `SPOTIFY_REDIRECT_URI` wordt bv. `https://jouw-app.onrender.com/callback`.
5. Voeg diezelfde Redirect URI ook toe in het Spotify Developer dashboard
   onder **Settings → Redirect URIs**.
6. Deploy. Open de Render-URL, log in en start het afwisselend afspelen —
   werkt dan overal, ook onderweg.

## Playlists invullen

Je kan een Spotify-link plakken zoals
`https://open.spotify.com/playlist/37i9dQZF1DXxxxx` — de app haalt zelf de
playlist-id eruit.

## Hoe het werkt

De app haalt alle nummers van beide playlists op, shuffelt ze (optioneel) en
weeft ze in elkaar volgens jullie patroon (bv. 1 kind / 1 volwassene). Die
volledige afspeellijst wordt in één keer naar Spotify Connect gestuurd zodat
het apparaat (de Tesla) 'm gewoon achter elkaar afspeelt — geen constante
verbinding met de server nodig tijdens het rijden.

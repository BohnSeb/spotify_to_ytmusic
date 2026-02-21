#!/usr/bin/env python3
#
#  This file is licensed under the MIT license
#  This file originates from https://github.com/caseychu/spotify-backup

import base64
import codecs
import hashlib
import http.server
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

# Default client_id from original spotify-backup; may be rate-limited or restricted.
# Prefer SPOTIFY_CLIENT_ID or spotify_client.json (see get_spotify_client_id).
_DEFAULT_SPOTIFY_CLIENT_ID = "5c098bcc800e45d49e476265bc9b6934"
_SPOTIFY_CLIENT_FILE = "spotify_client.json"
_SPOTIFY_CREDENTIALS_FILE = "spotify_credentials.json"


def get_spotify_client_id():
    """Return Spotify client_id from env or spotify_client.json, or default."""
    cid = os.environ.get("SPOTIFY_CLIENT_ID")
    if cid:
        return cid.strip()
    if os.path.exists(_SPOTIFY_CLIENT_FILE):
        try:
            with open(_SPOTIFY_CLIENT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            cid = data.get("client_id") or data.get("client_id_spotify")
            if cid:
                return cid.strip()
        except (json.JSONDecodeError, OSError):
            pass
    return _DEFAULT_SPOTIFY_CLIENT_ID


def _pkce_code_verifier(length=64):
    """Generate a PKCE code verifier (43–128 chars)."""
    return secrets.token_urlsafe(length)[:length]


def _pkce_code_challenge(verifier):
    """Generate S256 code challenge from verifier."""
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("utf-8")


def _load_spotify_credentials():
    """Load saved refresh_token and client_id. Returns (client_id, refresh_token) or (None, None)."""
    if not os.path.exists(_SPOTIFY_CREDENTIALS_FILE):
        return (None, None)
    try:
        with open(_SPOTIFY_CREDENTIALS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        cid = data.get("client_id")
        ref = data.get("refresh_token")
        if cid and ref:
            return (cid, ref)
    except (json.JSONDecodeError, OSError):
        pass
    return (None, None)


def _save_spotify_credentials(client_id, refresh_token):
    """Save OAuth credentials so we can refresh without opening the browser again."""
    try:
        with open(_SPOTIFY_CREDENTIALS_FILE, "w", encoding="utf-8") as f:
            json.dump({"client_id": client_id, "refresh_token": refresh_token}, f, indent=2)
    except OSError:
        pass


def _refresh_spotify_token(client_id, refresh_token):
    """Exchange refresh_token for a new access_token. Returns access_token or None."""
    data = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req) as res:
            reader = codecs.getreader("utf-8")
            body = json.load(reader(res))
        return body["access_token"]
    except (urllib.error.HTTPError, OSError, KeyError):
        return None


class SpotifyAPI:
    """Class to interact with the Spotify API using an OAuth token."""

    BASE_URL = "https://api.spotify.com/v1/"

    def __init__(self, auth):
        self._auth = auth

    def get(self, url, params={}, tries=3):
        """Fetch a resource from Spotify API."""
        url = self._construct_url(url, params)
        last_err = None
        for _ in range(tries):
            try:
                req = self._create_request(url)
                return self._read_response(req)
            except urllib.error.HTTPError as err:
                last_err = err
                body = ""
                try:
                    body = err.read().decode("utf-8", errors="replace")[:500]
                except Exception:
                    pass
                print(
                    f"Error fetching URL {url}: HTTP {err.code} {err.reason}"
                    + (f" — {body}" if body else "")
                )
                if err.code in (401, 403):
                    print(
                        "Spotify may have restricted this app. Use your own Spotify app: "
                        "create an app at developer.spotify.com, set Redirect URI to "
                        "http://127.0.0.1:43019/redirect, then set SPOTIFY_CLIENT_ID or create spotify_client.json with your client_id."
                    )
                time.sleep(2)
            except Exception as err:
                last_err = err
                print(f"Error fetching URL {url}: {err}")
                time.sleep(2)
        sys.exit(
            "Failed to fetch data from Spotify API after retries. "
            "If you see 401/403, use your own Spotify app credentials (SPOTIFY_CLIENT_ID or spotify_client.json)."
        )

    def list(self, url, params={}):
        """Fetch paginated resources and return as a combined list."""
        response = self.get(url, params)
        items = response["items"]

        while response["next"]:
            response = self.get(response["next"])
            items += response["items"]
        return items

    @staticmethod
    def authorize(client_id, scope):
        """Open a browser for user authorization (PKCE flow) and return SpotifyAPI instance."""
        redirect_uri = f"http://127.0.0.1:{SpotifyAPI._SERVER_PORT}/redirect"
        code_verifier = _pkce_code_verifier()
        code_challenge = _pkce_code_challenge(code_verifier)
        state = secrets.token_urlsafe(32)

        url = SpotifyAPI._construct_auth_url_pkce(
            client_id, scope, redirect_uri, code_challenge, state
        )
        print(f"Open this link if the browser doesn't open automatically: {url}")
        webbrowser.open(url)

        server = SpotifyAPI._AuthorizationServer(
            "127.0.0.1", SpotifyAPI._SERVER_PORT
        )
        server.code_verifier = code_verifier
        server.state = state
        server.client_id = client_id
        server.redirect_uri = redirect_uri
        try:
            while True:
                server.handle_request()
        except SpotifyAPI._Authorization as auth:
            if auth.refresh_token:
                _save_spotify_credentials(client_id, auth.refresh_token)
            return SpotifyAPI(auth.access_token)

    @staticmethod
    def _construct_auth_url_pkce(client_id, scope, redirect_uri, code_challenge, state):
        """Build authorize URL for Authorization Code with PKCE (response_type=code)."""
        return "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "scope": scope,
                "redirect_uri": redirect_uri,
                "code_challenge_method": "S256",
                "code_challenge": code_challenge,
                "state": state,
            }
        )

    def _construct_url(self, url, params):
        """Construct a full API URL."""
        if not url.startswith(self.BASE_URL):
            url = self.BASE_URL + url
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        return url

    def _create_request(self, url):
        """Create an authenticated request."""
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {self._auth}")
        return req

    def _read_response(self, req):
        """Read and parse the response."""
        with urllib.request.urlopen(req) as res:
            reader = codecs.getreader("utf-8")
            return json.load(reader(res))

    _SERVER_PORT = 43019

    class _AuthorizationServer(http.server.HTTPServer):
        def __init__(self, host, port):
            super().__init__((host, port), SpotifyAPI._AuthorizationHandler)

        def handle_error(self, request, client_address):
            raise

    class _AuthorizationHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/redirect"):
                self._handle_redirect()
            else:
                self.send_error(404)

        def _handle_redirect(self):
            # PKCE: Spotify redirects to /redirect?code=...&state=...
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)

            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<script>close()</script>Thanks! You may now close this window."
            )

            if "code" in params:
                code = params["code"][0]
                state = params.get("state", [None])[0]
                srv = self.server
                if getattr(srv, "state", None) and state != srv.state:
                    print("State mismatch (possible CSRF). Try again.", file=sys.stderr)
                    raise RuntimeError("State mismatch")
                access_token, refresh_token = self._exchange_code_for_token(
                    code,
                    srv.client_id,
                    srv.redirect_uri,
                    srv.code_verifier,
                )
                raise SpotifyAPI._Authorization(access_token, refresh_token)

            if "error" in params:
                err = params["error"][0]
                err_desc = params.get("error_description", [""])[0]
                msg = f"Spotify authorization failed: {err}. {err_desc}"
                print(msg, file=sys.stderr)
                raise RuntimeError(msg)

            print(
                "No code or error in redirect. Check redirect URI: http://127.0.0.1:43019/redirect",
                file=sys.stderr,
            )
            raise RuntimeError("No code in redirect")

        def _exchange_code_for_token(self, code, client_id, redirect_uri, code_verifier):
            """POST to Spotify token endpoint; return (access_token, refresh_token or None)."""
            data = urllib.parse.urlencode(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": client_id,
                    "code_verifier": code_verifier,
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                "https://accounts.spotify.com/api/token",
                data=data,
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with urllib.request.urlopen(req) as res:
                reader = codecs.getreader("utf-8")
                body = json.load(reader(res))
            return (body["access_token"], body.get("refresh_token"))

        def log_message(self, format, *args):
            pass

    class _Authorization(Exception):
        def __init__(self, access_token, refresh_token=None):
            self.access_token = access_token
            self.refresh_token = refresh_token


def fetch_user_data(spotify, dump):
    """Fetch playlists and liked songs based on the dump parameter."""
    playlists = []
    liked_albums = []

    if "liked" in dump:
        print("Loading liked albums and songs...")
        liked_tracks = spotify.list("me/tracks", {"limit": 50})
        liked_albums = spotify.list("me/albums", {"limit": 50})
        playlists.append({"name": "Liked Songs", "tracks": liked_tracks})

    if "playlists" in dump:
        print("Loading playlists...")
        playlist_data = spotify.list("me/playlists", {"limit": 50})
        for playlist in playlist_data:
            print(f"Loading playlist: {playlist['name']}")
            playlist["tracks"] = spotify.list(
                playlist["tracks"]["href"], {"limit": 100}
            )
        playlists.extend(playlist_data)

    return playlists, liked_albums


def write_to_file(file, format, playlists, liked_albums):
    """Write fetched data to a file in the specified format."""
    print(f"Writing to {file}...")
    with open(file, "w", encoding="utf-8") as f:
        if format == "json":
            json.dump({"playlists": playlists, "albums": liked_albums}, f)
        else:
            for playlist in playlists:
                f.write(playlist["name"] + "\r\n")
                for track in playlist["tracks"]:
                    if track["track"]:
                        f.write(
                            "{name}\t{artists}\t{album}\t{uri}\t{release_date}\r\n".format(
                                uri=track["track"]["uri"],
                                name=track["track"]["name"],
                                artists=", ".join(
                                    [
                                        artist["name"]
                                        for artist in track["track"]["artists"]
                                    ]
                                ),
                                album=track["track"]["album"]["name"],
                                release_date=track["track"]["album"]["release_date"],
                            )
                        )
                f.write("\r\n")


def main(dump="playlists,liked", format="json", file="playlists.json", token=""):
    print("Starting backup...")
    client_id = get_spotify_client_id()
    if client_id == _DEFAULT_SPOTIFY_CLIENT_ID:
        print(
            "Using default Spotify app. If backup fails with 401/403, create your own app at "
            "developer.spotify.com and set SPOTIFY_CLIENT_ID or spotify_client.json (see README)."
        )
    if token:
        spotify = SpotifyAPI(token)
    else:
        saved_client_id, saved_refresh_token = _load_spotify_credentials()
        if saved_client_id and saved_refresh_token:
            access_token = _refresh_spotify_token(saved_client_id, saved_refresh_token)
            if access_token:
                spotify = SpotifyAPI(access_token)
            else:
                spotify = SpotifyAPI.authorize(
                    client_id=client_id,
                    scope="playlist-read-private playlist-read-collaborative user-library-read",
                )
        else:
            spotify = SpotifyAPI.authorize(
                client_id=client_id,
                scope="playlist-read-private playlist-read-collaborative user-library-read",
            )

    playlists, liked_albums = fetch_user_data(spotify, dump)
    write_to_file(file, format, playlists, liked_albums)
    print(f"Backup completed! Data written to {file}")


if __name__ == "__main__":
    main()

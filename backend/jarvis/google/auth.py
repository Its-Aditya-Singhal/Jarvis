"""Google sign-in with the owner's own OAuth "Desktop app" client.

Google gives no API keys for Gmail, Drive or Calendar, so the owner creates a free OAuth client
in Google Cloud Console (Settings → Accounts walks through it) and pastes its ID and secret.
Connect then runs the installed-app flow:

1. a one-shot HTTP server on a random 127.0.0.1 port (separate from the JARVIS API, so its
   token and Origin checks never see Google's redirect) waits for the browser to come back;
2. the browser opens Google's consent page with a PKCE challenge and a random ``state``;
3. the redirect's ``state`` is checked, the code is exchanged (with the PKCE verifier) at
   ``oauth2.googleapis.com/token``, and the refresh token is sealed with the Keychain key.

Access tokens live in memory only and are refreshed when they run out. Only the scopes of the
services switched on are requested. Disconnect revokes the token at Google and deletes it here.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import re
import secrets
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from ..database.db import Database
from ..security.secrets import Secrets

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
CONSOLE_URL = "https://console.cloud.google.com/apis/credentials"
# what offline mode lets through once an account is connected (or while connecting)
HOSTS = ("oauth2.googleapis.com", "accounts.google.com", "gmail.googleapis.com", "www.googleapis.com")

SERVICES = ("gmail", "drive", "calendar")
_S = "https://www.googleapis.com/auth/"
SCOPES: dict[str, tuple[str, ...]] = {
    # read mail, save drafts, and send (only ever after the owner confirms the read-back)
    "gmail": (_S + "gmail.readonly", _S + "gmail.compose", _S + "gmail.send"),
    "drive": (_S + "drive.readonly",),
    "calendar": (_S + "calendar.events",),
}
IDENTITY = ("openid", "email")  # only to show which account is connected

CLIENT_ID = "google_client_id"  # names in the sealed secrets store
CLIENT_SECRET = "google_client_secret"
REFRESH = "google_refresh_token"
CLIENT_ID_RE = re.compile(r"^[0-9]{6,30}-[a-z0-9]{10,64}\.apps\.googleusercontent\.com$")
CLIENT_SECRET_RE = re.compile(r"^[A-Za-z0-9_\-]{10,100}$")
CONNECT_TIMEOUT_S = 300.0
REFRESH_MARGIN_S = 60.0

PAGE = """<!doctype html><meta charset="utf-8"><title>JARVIS</title>
<body style="font-family:-apple-system,system-ui,sans-serif;background:#0b1220;color:#d8e6ff;
display:flex;align-items:center;justify-content:center;height:90vh"><div style="text-align:center">
<h2>{title}</h2><p>{text}</p></div></body>"""


class GoogleError(Exception):
    """Something the owner should hear about, worded to be spoken."""


class NotConnected(GoogleError):
    pass


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge) as RFC 7636 describes."""
    verifier = _b64url(secrets.token_bytes(48))  # 64 characters
    return verifier, _b64url(hashlib.sha256(verifier.encode()).digest())


def parse_client_json(text: str) -> tuple[str, str]:
    """The client ID and secret from the JSON file Google Cloud Console downloads."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError("that isn't the JSON file Google Cloud Console downloads") from exc
    block = data.get("installed") or data.get("web") if isinstance(data, dict) else None
    if not isinstance(block, dict) or "client_id" not in block:
        raise ValueError("that JSON has no OAuth client in it (create an OAuth client ID of type Desktop app)")
    if "web" in data and "installed" not in data:
        raise ValueError("that's a Web application client; create one of type Desktop app instead")
    return str(block.get("client_id") or ""), str(block.get("client_secret") or "")


def _email_from_id_token(token: str) -> str:
    """The account's address from the ID token Google's token endpoint just returned over TLS
    (Google's docs allow reading it without checking the signature in that case)."""
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return str(data.get("email") or "")[:200]
    except (IndexError, ValueError):
        return ""


class GoogleAuth:
    def __init__(self, db: Database, secrets_: Secrets, transport: httpx.BaseTransport | None = None,
                 on_change: Callable[[], None] = lambda: None, timeout_s: float = 20.0):
        self.db = db
        self.secrets = secrets_
        self.on_change = on_change
        self._http = httpx.Client(timeout=httpx.Timeout(timeout_s, connect=5.0), transport=transport)
        self._lock = threading.Lock()
        self._token: tuple[str, float] | None = None  # (access token, monotonic expiry)
        self._flow: dict[str, Any] | None = None  # the connect in progress
        self.error = ""  # why the last connect failed

    # -- configuration -------------------------------------------------------------------
    @property
    def client_id(self) -> str:
        return self.secrets.get(CLIENT_ID) or ""

    def set_client(self, client_id: str, client_secret: str) -> None:
        client_id, client_secret = client_id.strip(), client_secret.strip()
        if not CLIENT_ID_RE.match(client_id):
            raise ValueError("that isn't an OAuth client ID (it ends in .apps.googleusercontent.com)")
        if not CLIENT_SECRET_RE.match(client_secret):
            raise ValueError("that isn't an OAuth client secret (it usually starts with GOCSPX-)")
        if self.connected and client_id != self.client_id:
            self.disconnect()  # tokens belong to the old client
        self.secrets.set(CLIENT_ID, client_id)
        self.secrets.set(CLIENT_SECRET, client_secret)
        self.db.add_security_event("settings_changed", "Google OAuth client saved")

    def remove_client(self) -> None:
        if self.connected:
            self.disconnect()
        self.secrets.set(CLIENT_ID, None)
        self.secrets.set(CLIENT_SECRET, None)
        self.db.add_security_event("settings_changed", "Google OAuth client removed")

    def wanted(self) -> list[str]:
        raw = self.db.get("google.services")
        if raw is None:
            return list(SERVICES)
        return [s for s in raw.split(",") if s in SERVICES]

    def set_wanted(self, services: list[str]) -> None:
        self.db.set("google.services", ",".join(s for s in SERVICES if s in services))

    def granted(self) -> set[str]:
        return set((self.db.get("google.scopes") or "").split())

    @property
    def connected(self) -> bool:
        return self.secrets.has(REFRESH)

    @property
    def connecting(self) -> bool:
        f = self._flow
        return f is not None and time.monotonic() < f["deadline"]

    def has(self, service: str) -> bool:
        """Connected, switched on, and Google granted every scope the service needs."""
        return (self.connected and service in self.wanted()
                and all(s in self.granted() for s in SCOPES[service]))

    def hosts(self) -> tuple[str, ...]:
        return HOSTS if (self.connected or self.connecting) else ()

    def status(self) -> dict:
        granted = self.granted()
        return {
            "client_id": self.client_id or None,  # not a secret: it appears in every consent URL
            "has_secret": self.secrets.has(CLIENT_SECRET),
            "services": {s: s in self.wanted() for s in SERVICES},
            "connected": self.connected,
            "email": self.db.get("google.email") or None,
            "granted": {s: all(x in granted for x in SCOPES[s]) for s in SERVICES} if self.connected else {},
            "connecting": self.connecting,
            "error": self.error or None,
            "console_url": CONSOLE_URL,
        }

    # -- connect ---------------------------------------------------------------------------
    def begin(self) -> str:
        """Start the loopback server and return the consent URL for the browser."""
        cid, secret = self.client_id, self.secrets.get(CLIENT_SECRET)
        if not cid or not secret:
            raise GoogleError("paste the OAuth client ID and secret first")
        wanted = self.wanted()
        if not wanted:
            raise GoogleError("switch on at least one of Gmail, Drive or Calendar first")
        self.cancel()
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(24)
        server = HTTPServer(("127.0.0.1", 0), self._handler())
        server.timeout = 0.5
        redirect = f"http://127.0.0.1:{server.server_address[1]}"
        scopes = [*IDENTITY, *(s for svc in wanted for s in SCOPES[svc])]
        flow = {"state": state, "verifier": verifier, "redirect": redirect, "server": server,
                "deadline": time.monotonic() + CONNECT_TIMEOUT_S, "done": threading.Event()}
        self._flow = flow
        self.error = ""
        threading.Thread(target=self._serve, args=(flow,), name="google-oauth", daemon=True).start()
        return AUTH_URL + "?" + urlencode({
            "client_id": cid, "redirect_uri": redirect, "response_type": "code", "scope": " ".join(scopes),
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
            "access_type": "offline", "prompt": "consent",  # always a refresh token, even on a reconnect
        })

    def cancel(self) -> None:
        f, self._flow = self._flow, None
        if f is not None:
            f["done"].set()

    def _serve(self, flow: dict) -> None:
        server: HTTPServer = flow["server"]
        try:
            while not flow["done"].is_set() and time.monotonic() < flow["deadline"]:
                server.handle_request()
        finally:
            server.server_close()
            if self._flow is flow:
                self._flow = None
                if not flow["done"].is_set():
                    self.error = "Google sign-in timed out — press Connect to try again"
                self.on_change()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        auth = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:  # the query holds the code: never log it
                pass

            def do_GET(self) -> None:
                url = urlparse(self.path)
                if url.path != "/":
                    self.send_error(404)
                    return
                ok, title, text = auth._redirected(parse_qs(url.query))
                body = PAGE.format(title=html.escape(title), text=html.escape(text)).encode()
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler

    def _redirected(self, q: dict[str, list[str]]) -> tuple[bool, str, str]:
        """Google sent the browser back: check it, exchange the code, finish the connect."""
        flow = self._flow
        one = lambda k: (q.get(k) or [""])[0]
        if flow is None:
            return False, "Nothing to finish", "No Google sign-in is in progress. Press Connect in JARVIS again."
        if not hmac.compare_digest(one("state").encode(), flow["state"].encode()):
            # someone else's redirect (or a forged one): the flow stays open for the real one
            log.warning("google sign-in: redirect with a wrong state ignored")
            return False, "Sign-in not accepted", "This link doesn't belong to the sign-in JARVIS started."
        flow["done"].set()
        try:
            if one("error"):
                raise GoogleError("you didn't allow access" if one("error") == "access_denied"
                                  else f"Google said: {one('error')[:80]}")
            if not one("code"):
                raise GoogleError("Google sent no sign-in code")
            self._exchange(one("code"), flow)
        except GoogleError as exc:
            self.error = f"Couldn't connect: {exc}"
            self.db.add_security_event("google_connect_failed", self.error)
            self.on_change()
            return False, "Not connected", f"{exc}. You can close this tab and try again in JARVIS."
        self.on_change()
        return True, "Connected", "JARVIS can now use your Google account. You can close this tab."

    def _exchange(self, code: str, flow: dict) -> None:
        data = self._token_call({
            "grant_type": "authorization_code", "code": code, "code_verifier": flow["verifier"],
            "redirect_uri": flow["redirect"], "client_id": self.client_id,
            "client_secret": self.secrets.get(CLIENT_SECRET) or "",
        })
        refresh = str(data.get("refresh_token") or "")
        if not refresh:
            raise GoogleError("Google sent no refresh token")
        self.secrets.set(REFRESH, refresh)
        self.db.set("google.scopes", str(data.get("scope") or ""))
        self.db.set("google.email", _email_from_id_token(str(data.get("id_token") or "")))
        self._keep(data)
        missing = [s for s in self.wanted() if not all(x in self.granted() for x in SCOPES[s])]
        self.error = (f"Connected, but Google didn't grant {', '.join(missing)} (tick every box on the consent "
                      "page, then Connect again)") if missing else ""
        self.db.add_security_event("google_connected", "Google account connected ("
                                   + ", ".join(s for s in self.wanted() if s not in missing) + ")")

    # -- tokens ------------------------------------------------------------------------------
    def _token_call(self, form: dict[str, str]) -> dict:
        try:
            r = self._http.post(TOKEN_URL, data=form)
        except httpx.HTTPError as exc:
            raise GoogleError(f"Google isn't reachable ({exc.__class__.__name__})") from exc
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code != 200 or not isinstance(data, dict):
            err = str(data.get("error") or r.status_code) if isinstance(data, dict) else str(r.status_code)
            if err == "invalid_grant":
                raise NotConnected("Google sign-in has expired or was revoked — reconnect in Settings → Accounts")
            if err == "invalid_client":
                raise GoogleError("Google didn't accept the OAuth client ID or secret")
            raise GoogleError(f"Google refused the sign-in ({err[:60]})")
        return data

    def _keep(self, data: dict) -> str:
        token = str(data.get("access_token") or "")
        if not token:
            raise GoogleError("Google sent no access token")
        try:
            life = float(data.get("expires_in") or 3600)
        except (TypeError, ValueError):
            life = 3600.0
        self._token = (token, time.monotonic() + life)
        return token

    def access_token(self) -> str:
        """A valid access token (refreshed when it is about to run out). Raises NotConnected."""
        with self._lock:
            t = self._token
            if t is not None and time.monotonic() < t[1] - REFRESH_MARGIN_S:
                return t[0]
            refresh = self.secrets.get(REFRESH)
            if not refresh:
                raise NotConnected("your Google account isn't connected — connect it in Settings → Accounts")
            try:
                data = self._token_call({"grant_type": "refresh_token", "refresh_token": refresh,
                                         "client_id": self.client_id,
                                         "client_secret": self.secrets.get(CLIENT_SECRET) or ""})
            except NotConnected:
                self._forget("Google sign-in expired or revoked")
                raise
            if data.get("scope"):
                self.db.set("google.scopes", str(data["scope"]))
            return self._keep(data)

    def invalidate(self) -> None:
        """The API refused the cached token (401): the next call refreshes it."""
        with self._lock:
            self._token = None

    def _forget(self, why: str) -> None:
        self._token = None
        self.secrets.set(REFRESH, None)
        self.db.set("google.scopes", "")
        self.db.set("google.email", "")
        self.db.add_security_event("google_disconnected", why)
        self.on_change()

    def disconnect(self) -> None:
        """Revoke the token at Google (best effort: offline, it still goes) and delete it here."""
        self.cancel()
        refresh = self.secrets.get(REFRESH)
        if refresh:
            try:
                self._http.post(REVOKE_URL, data={"token": refresh})
            except httpx.HTTPError as exc:
                log.warning("google: revoke failed (%s); deleted locally", exc.__class__.__name__)
        self.error = ""
        self._forget("Google account disconnected")

    # -- API calls -------------------------------------------------------------------------------
    def call(self, method: str, url: str, service: str, **kw: Any) -> httpx.Response:
        """One Google API request with the owner's token; errors become spoken-friendly GoogleErrors."""
        if not self.has(service):
            name = {"gmail": "Gmail", "drive": "Google Drive", "calendar": "Google Calendar"}[service]
            if self.connected and service in self.wanted():
                raise NotConnected(f"{name} wasn't allowed when you connected — reconnect in Settings → Accounts")
            raise NotConnected(f"{name} isn't connected — connect it in Settings → Accounts")
        extra = kw.pop("headers", {})
        for attempt in (0, 1):
            headers = {**extra, "Authorization": f"Bearer {self.access_token()}"}
            try:
                r = self._http.request(method, url, headers=headers, **kw)
            except httpx.HTTPError as exc:
                raise GoogleError(f"I can't reach Google right now ({exc.__class__.__name__})") from exc
            if r.status_code == 401 and attempt == 0:
                self.invalidate()
                continue
            if r.status_code < 400:
                return r
            try:
                msg = str(((r.json() or {}).get("error") or {}).get("message") or "")
            except (ValueError, AttributeError):
                msg = ""
            if r.status_code == 403 and ("scope" in msg.lower() or "insufficient" in msg.lower()):
                raise NotConnected("Google didn't allow that — reconnect in Settings → Accounts")
            if r.status_code == 403 and ("has not been used" in msg.lower() or "is disabled" in msg.lower()):
                raise GoogleError("that API isn't enabled in your Google Cloud project — enable it in the Console")
            if r.status_code == 429:
                raise GoogleError("Google says too many requests; try again in a minute")
            if r.status_code == 404:
                raise GoogleError("Google couldn't find that")
            raise GoogleError(f"Google returned an error ({r.status_code})")
        raise GoogleError("Google kept refusing the sign-in token")

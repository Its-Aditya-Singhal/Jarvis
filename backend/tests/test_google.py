"""Google sign-in: loopback redirect with PKCE and state, sealed refresh token, scopes per service."""

import base64
import hashlib
import json
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from jarvis.database.db import Database
from jarvis.google.auth import (
    CLIENT_SECRET,
    HOSTS,
    REFRESH,
    SCOPES,
    GoogleAuth,
    GoogleError,
    NotConnected,
    parse_client_json,
)
from jarvis.security.crypto import StaticKeyProvider
from jarvis.security.secrets import Secrets

# made-up values, assembled so secret scanners don't mistake them for real credentials
CID = "-".join(["123456789012", "fakeclient" * 3]) + ".apps.googleusercontent.com"
SECRET = "-".join(["GOCSPX", "fake" * 6])
ALL = " ".join(s for v in SCOPES.values() for s in v)


def id_token(email: str) -> str:
    part = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{part({'alg': 'RS256'})}.{part({'email': email})}.sig"


class Google:
    """A scripted token endpoint (and any API URL) for ``httpx.MockTransport``."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.forms: list[dict] = []
        self.scope = ALL
        self.refresh_ok = True
        self.api: dict[str, list] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = str(request.url)
        if url.startswith("https://oauth2.googleapis.com/token"):
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.forms.append(form)
            if form["grant_type"] == "refresh_token" and not self.refresh_ok:
                return httpx.Response(400, json={"error": "invalid_grant"})
            out = {"access_token": f"ya29.token{len(self.forms)}", "expires_in": 3599, "scope": self.scope}
            if form["grant_type"] == "authorization_code":
                out |= {"refresh_token": "1//refresh-secret", "id_token": id_token("aditya@gmail.com")}
            return httpx.Response(200, json=out)
        if url.startswith("https://oauth2.googleapis.com/revoke"):
            return httpx.Response(200)
        for prefix, replies in self.api.items():
            if url.startswith(prefix):
                r = replies.pop(0)
                return r(request) if callable(r) else r
        return httpx.Response(404, json={"error": {"message": "not scripted"}})


@pytest.fixture
def google(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    fake = Google()
    changes = []
    g = GoogleAuth(db, Secrets(db, StaticKeyProvider()), transport=httpx.MockTransport(fake),
                   on_change=lambda: changes.append(1))
    g.fake, g.changes = fake, changes  # type: ignore[attr-defined]
    yield g
    g.cancel()
    db.close()


def browser_returns(url: str, **override) -> tuple[int, str]:
    """What the browser does after the consent page: GET the loopback redirect."""
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    params = {"state": q["state"], "code": "4/auth-code", **override}
    target = q["redirect_uri"] + "/?" + "&".join(f"{k}={v}" for k, v in params.items())
    try:
        with urllib.request.urlopen(target, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def connect(g: GoogleAuth) -> str:
    g.set_client(CID, SECRET)
    url = g.begin()
    status, page = browser_returns(url)
    assert status == 200 and "Connected" in page
    return url


def test_client_json_from_cloud_console():
    cid, secret = parse_client_json(json.dumps({"installed": {"client_id": CID, "client_secret": SECRET,
                                                              "redirect_uris": ["http://localhost"]}}))
    assert (cid, secret) == (CID, SECRET)
    with pytest.raises(ValueError, match="Desktop app"):
        parse_client_json(json.dumps({"web": {"client_id": CID, "client_secret": SECRET}}))
    with pytest.raises(ValueError):
        parse_client_json("not json")


def test_bad_client_values_are_refused(google):
    with pytest.raises(ValueError, match="client ID"):
        google.set_client("hello", SECRET)
    with pytest.raises(ValueError, match="secret"):
        google.set_client(CID, "x y")
    with pytest.raises(GoogleError, match="client ID and secret"):
        google.begin()


def test_connect_uses_pkce_state_and_only_the_wanted_scopes(google):
    google.set_wanted(["gmail", "calendar"])
    google.set_client(CID, SECRET)
    url = google.begin()
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert q["redirect_uri"].startswith("http://127.0.0.1:") and q["code_challenge_method"] == "S256"
    assert q["access_type"] == "offline" and q["client_id"] == CID and SECRET not in url
    scopes = q["scope"].split()
    assert "https://www.googleapis.com/auth/gmail.send" in scopes and "https://www.googleapis.com/auth/calendar.events" in scopes
    assert not any("drive" in s for s in scopes)
    assert google.connecting and google.hosts() == HOSTS  # offline mode lets the token exchange through
    assert browser_returns(url)[0] == 200
    form = google.fake.forms[0]
    # the verifier sent at the exchange matches the challenge in the consent URL
    digest = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    assert digest == q["code_challenge"] and form["code"] == "4/auth-code" and form["redirect_uri"] == q["redirect_uri"]
    st = google.status()
    assert st["connected"] and st["email"] == "aditya@gmail.com" and st["error"] is None
    assert google.has("gmail") and google.has("calendar") and not google.has("drive")
    assert google.changes  # the UI was told


def test_a_redirect_with_the_wrong_state_is_ignored(google):
    google.set_client(CID, SECRET)
    url = google.begin()
    status, page = browser_returns(url, state="forged")
    assert status == 400 and not google.connected and google.connecting
    assert google.fake.forms == []  # no code exchange for a forged redirect
    assert browser_returns(url)[0] == 200 and google.connected  # the real one still works


def test_denied_consent_is_reported(google):
    google.set_client(CID, SECRET)
    url = google.begin()
    status, _ = browser_returns(url, error="access_denied", code="")
    assert status == 400 and not google.connected and "didn't allow" in google.status()["error"]


def test_refresh_token_is_sealed_and_never_in_status(google, tmp_path):
    connect(google)
    raw = json.dumps(google.status())
    assert "refresh-secret" not in raw and SECRET not in raw
    stored = google.db.get("secret." + REFRESH)
    assert stored and "refresh-secret" not in stored and google.db.get("secret." + CLIENT_SECRET) != SECRET


def test_access_token_is_cached_and_refreshed(google):
    connect(google)
    assert google.access_token() == "ya29.token1"  # from the exchange, no new request
    google._token = ("old", 0.0)  # expired
    assert google.access_token() == "ya29.token2"
    assert google.fake.forms[-1]["grant_type"] == "refresh_token"


def test_a_revoked_token_disconnects(google):
    connect(google)
    google._token = None
    google.fake.refresh_ok = False
    with pytest.raises(NotConnected, match="reconnect"):
        google.access_token()
    assert not google.connected and google.hosts() == ()


def test_missing_scopes_are_reported(google):
    google.fake.scope = "openid email " + " ".join(SCOPES["calendar"])
    connect(google)
    assert google.has("calendar") and not google.has("gmail")
    assert "didn't grant gmail, drive" in google.status()["error"]
    with pytest.raises(NotConnected, match="wasn't allowed"):
        google.call("GET", "https://gmail.googleapis.com/gmail/v1/users/me/messages", "gmail")


def test_api_calls_carry_the_token_and_retry_once_on_401(google):
    connect(google)
    url = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
    google.fake.api[url] = [httpx.Response(401), httpx.Response(200, json={"items": []})]
    r = google.call("GET", url, "calendar")
    assert r.json() == {"items": []}
    sent = [c for c in google.fake.calls if str(c.url).startswith(url)]
    assert sent[0].headers["authorization"] == "Bearer ya29.token1"
    assert sent[1].headers["authorization"] == "Bearer ya29.token2"  # refreshed after the 401


def test_disconnect_revokes_and_deletes(google):
    connect(google)
    google.disconnect()
    assert any(str(c.url).startswith("https://oauth2.googleapis.com/revoke") for c in google.fake.calls)
    assert not google.connected and google.status()["email"] is None
    with pytest.raises(NotConnected):
        google.call("GET", "https://www.googleapis.com/drive/v3/files", "drive")


def test_connect_api_needs_level_two_and_reports_status(tmp_path):
    import time

    from test_api import H, _client, _voice_ready
    from test_voice_only import _enroll_voice, _voice_only

    s = _voice_only(tmp_path)
    keys = StaticKeyProvider()
    client, svc = _client(s, keys=keys, google=None)
    with client:
        _voice_ready(svc)
        assert client.get("/api/settings/google", headers=H).status_code == 403  # nobody verified yet
        client.post("/api/setup/profile", headers=H, json={"owner_name": "Aditya", "assistant_name": "JARVIS"})
        _enroll_voice(svc)
        assert client.post("/api/setup/complete", headers=H).status_code == 200
        st = client.get("/api/settings/google", headers=H).json()
        assert st["connected"] is False and st["services"] == {"gmail": True, "drive": True, "calendar": True}
        assert st["client_id"] is None and st["has_secret"] is False
        svc.prefs.set("security.typed", "off")
        body = {"client_json": json.dumps({"installed": {"client_id": CID, "client_secret": SECRET}}),
                "services": ["gmail", "drive"]}
        assert client.put("/api/settings/google", headers=H, json=body).status_code == 403  # level 2 = the voice
        svc.voice.auth.judge(0.9, 0.9, time.monotonic(), 2.0)
        r = client.put("/api/settings/google", headers=H, json=body)
        assert r.status_code == 200, r.text
        assert r.json()["client_id"] == CID and r.json()["has_secret"] and SECRET not in r.text
        assert r.json()["services"] == {"gmail": True, "drive": True, "calendar": False}
        bad = client.put("/api/settings/google", headers=H, json={"client_id": "nope", "client_secret": SECRET})
        assert bad.status_code == 400
        r = client.post("/api/settings/google/connect", headers=H)
        assert r.status_code == 200 and r.json()["url"].startswith("https://accounts.google.com/")
        assert r.json()["connecting"] is True
        assert set(HOSTS) <= set(svc.guard.services())  # offline mode lets the sign-in through
        client.post("/api/settings/google/cancel", headers=H)
        assert svc.google.connecting is False and not set(HOSTS) & set(svc.guard.services())

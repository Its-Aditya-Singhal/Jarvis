"""Google accounts: Gmail, Google Drive and Google Calendar over their REST APIs (httpx only).

``auth.GoogleAuth`` signs in with the owner's own OAuth "Desktop app" client (loopback redirect
with PKCE) and keeps the refresh token sealed with the Keychain key; ``gmail``, ``drive`` and
``gcal`` are thin clients that ask it for an access token on every call.
"""

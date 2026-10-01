# Security

JARVIS is a voice assistant that can change things on your Mac, so it keeps
checking who is in front of it. This page says what it protects against, how,
and where that stops. Read "Voice-only by default" and "Known limits" first.

## Voice-only by default

JARVIS ships **voice-only**: the camera, the face and anti-spoof models and the
face loop never start (the camera isn't even opened for a permission prompt).
That keeps it light enough to run all day. The trade-off is plain: your voice is
the only thing that says it's you, so the voice check is stricter than before
and it runs on every request. Face sign-in with liveness is still available
(`JARVIS_FACE_AUTH=1`); the face-specific rules below apply only then.

## Threat model

| Who | Example | What stops them |
|---|---|---|
| Someone else talking to your Mac | a flatmate says "JARVIS, read my email" | their voice is judged on that request: a rejected voice gets nothing (not even "louder") and is logged; an unclear one gets only harmless everyday commands |
| Someone talking during your conversation | the TV or a friend during the follow-up window | every follow-up is judged too; another voice closes the window without running anything |
| Someone at your unlocked keyboard | typing "send an email to…" in the command bar | typed commands are off by default (Settings → Security → Commands); with them off, typing gets only the harmless everyday commands (nothing that reads your data, asks the AI or acts), and a click on Confirm still needs your voice in the last minute |
| A recording of your voice | a clip of you played from a phone | **not stopped by voice-only sign-in** (see Known limits). Sending mail and deleting still need a fresh, explicit "yes" in your voice or a click, and every action is logged |
| Other programs on the Mac | a web page or local app calling the backend | the API binds to 127.0.0.1, needs a fresh random token each launch, rejects foreign Host and Origin headers (DNS rebinding); the Google sign-in redirect uses its own one-shot server with a checked `state` |
| The AI itself | a mail whose text tells the AI to forward your inbox | the AI only proposes structured tool calls; code validates each one, re-checks your voice before acting and composes the reply; sending a mail always reads it back and waits for your "yes"; there is no shell tool |
| Someone who copies your disk | the app's data folder on a stolen backup | voiceprint, memory, history, notes, events, the Gemini key and the Google tokens are sealed with AES-256-GCM; the key is in the macOS Keychain |
| The network | a library phoning home | the offline guard refuses every non-loopback connection from the backend except the services you switched on, and lists attempts |

**Out of scope:** an attacker with your macOS login or admin rights (they can
read the Keychain and the app's memory), a malicious macOS or hardware, and
physical coercion.

## Levels

Every action is authorised from live evidence at the moment it runs:

| Level | Needs (voice-only) | Allows |
|---|---|---|
| L0 | setup not finished | nothing |
| L1 READ | setup done; for a spoken request, a voice that wasn't rejected | harmless everyday commands with an unclear voice; with a recognised voice also reading calendar, notes, files, memory |
| L2 ACT | **this request** recognised as your voice, plus a match in the last 60 s and no unknown voice since | Gmail, Drive and Google Calendar, notes, apps, settings, typing, generated AppleScript |
| L3 CONFIRM | L2 + an explicit yes (in your recognised voice, or a click) before the card expires | sending a mail, deleting notes, events, memories, moving files to the Trash, privacy actions |

**Every request is judged on its own words.** Until this version a voice match
counted for a minute and everyday commands ran even when the voice was
rejected. Now:

- The speaker model runs on every utterance addressed to JARVIS, follow-ups in
  the conversation window included.
- A clip too short to judge (under 0.8 s of speech, like "louder") is always
  unclear, and it is never joined to the clip before it: your "Jarvis" plus
  someone else's quick "read my mail" could otherwise pass as you. A short
  "yes" for a confirmation is unclear too: say "yes, send it" so there is
  enough of your voice.
- **Recognised:** everything runs at its level. **Unclear** (too short, noisy,
  or between the thresholds): only the harmless everyday set (volume,
  brightness, dark mode, music, timers, alarms, the stopwatch, screenshots,
  web searches, the time, sums); nothing that reads your data or acts.
  **Rejected:** nothing at all, logged as a security event.
- Even a level-2 action inside a command is refused if that command's own
  verdict wasn't "recognised"; a spoken delay ("in 5 minutes…") can't carry a
  level-2 action past an unclear voice either.
- The **Strict** preset (accept at cosine 0.58, reject below 0.35) is the
  default. `scripts/eval_voice.py` measures false accepts and false rejects
  from your recordings and other voices, so the thresholds can be checked on
  your own voice rather than assumed.
- Enrollment is 24 phrases in three languages, at different volumes and
  distances, each checked for the right words, quality and consistency with
  the earlier ones; the voiceprint keeps every phrase plus 2-second windows.

**Typed commands (off by default).** Without a face check, the keyboard would
bypass the voice. With Settings → Commands on "Voice or typed", a typed command
or a click on Confirm counts like your voice. Leave it off unless the Mac is
only ever unlocked by you.

**With face sign-in on** (`JARVIS_FACE_AUTH=1`): L1 also needs your face over
several frames and passed liveness, another face in view caps you at L1, and
deletions need a liveness check in the last 10 minutes; the fusion classifier
can lower a level. See [ML.md](ML.md#how-auth-levels-and-fusion-work).

Level-3 tools are never run directly: the runner plans them, shows exactly what
will be affected, and executes only after the confirmation. A confirmation is
single-use and bound to that plan.

## The AI and your Google account

**Gemini (the brain).** Everyday commands are understood on the Mac by a
pattern matcher and never sent anywhere. Anything else is sent to Google's
Gemini API: the request's text, the time, your and the assistant's names, the
last few exchanges of this conversation (with what each command did), and any
remembered facts that match. For mail, files and calendar requests the writing
model also gets what the request needs: the headers and previews of the mails
being summarised, the one mail being read, the document being summarised.
Nothing else from your account is sent. On the free tier **Google may use
these requests to improve its products** and human reviewers may read them;
don't use JARVIS's AI for anything you wouldn't put in a Google search. The
local Ollama brain (Settings → AI → Local) keeps all of it on the Mac, at the
cost of several GB of memory.

**The API key** is sealed with the Keychain key in the database, never logged,
never sent back to the UI (only its last four characters), and sent only in
the `x-goog-api-key` header to `generativelanguage.googleapis.com`.

**Google sign-in.** You create your own OAuth client, so no third party is in
between. The sign-in uses the installed-app flow: a one-shot HTTP server on a
random 127.0.0.1 port, PKCE (S256), a random `state` that is checked before
the code is exchanged, and `prompt=consent`. Only the scopes of the services
you ticked are requested (`gmail.readonly`, `gmail.compose`, `gmail.send`,
`drive.readonly`, `calendar.events`, plus `openid email` to show the account).
The refresh token and the client secret are sealed with the Keychain key;
access tokens live in memory only. A revoked or expired sign-in is detected
and deleted. Disconnect and factory reset revoke the token at Google.

**Sending mail** is level 3: the mail is written, saved as a Gmail draft, read
back aloud in full and shown in the confirmation card, and sent only after a
"yes" in your recognised voice or a click on Send. A mail's own text can't
send anything: the AI only ever sees it as data to summarise.

**Offline mode** (on by default) still blocks everything except: the Gemini
API host once a key is saved, and `oauth2.googleapis.com`,
`accounts.google.com`, `gmail.googleapis.com` and `www.googleapis.com` once a
Google account is connected (and while you connect). Each connection is listed
in the Privacy view.

## Protections in detail

- Biometric templates are embeddings only (no photos), sealed with AES-256-GCM.
  The key lives in the macOS Keychain, so a copied template file can't be read.
  Tampering is detected on load.
- Biometrics are identifiers, not passwords. They can't be changed if leaked,
  which is why they never leave the device.
- The backend binds to 127.0.0.1 and requires a random token for each launch.
- Once setup is complete, the setup endpoints are locked. A face scan then
  needs a level-3 confirmation, or the owner's verified voice when the face
  profile is gone.
- Audio is processed in memory and discarded. Only voice embeddings are kept,
  sealed the same way as face templates.
- Memory (facts, conversation history, their embeddings) is sealed the same
  way. Fusion samples hold scores only. Recall matches words on the Mac by
  default; memory suggestions (off by default) and remembered facts relevant
  to a question go to the AI with that question (see above).
- Security events: unknown face or voice, spoof suspected, liveness check
  failed, liveness lockout, frozen camera feed, voice command while not
  verified, command in a non-owner voice, and a tool blocked because the
  level was too low (or the owner left mid-command). Confirmed deletions,
  privacy actions, security-setting changes, face re-scans, factory resets
  and fusion retraining are also logged. Each event records the time, the
  outcome, and whether access was blocked.
- **Any command (generated AppleScript).** For requests no built-in tool
  covers, the local model writes an AppleScript. Code, not the model, decides
  what happens to it (`jarvis/tools/agent.py`):
  - *Blocked, never run:* `do shell script`, Terminal / iTerm / Script Editor /
    Automator / Shortcuts, `run script` / `load script`, raw «event» codes, the
    Objective-C bridge, JavaScript in web pages, writing files, deleting or
    moving files through Finder or System Events, emptying the Trash,
    passwords and the Keychain, administrator rights, and apps named
    indirectly (`tell application someVariable`). A blocked script is logged
    as a security event.
  - *Read:* the script only asks apps for information, and both the model and
    the code agree. It runs straight away (level 2).
  - *Change:* everything else. The full script is shown in the confirmation
    card and runs only after a level-3 "yes" or Confirm click.
  - On an 8 GB Mac (the 3B model) every script needs confirmation; Settings can
    make that the rule everywhere, or turn scripts off. The script is compiled
    before it is shown (a compile error goes back to the model once), checked
    again right before it runs, limited to 4,000 characters and 30 seconds,
    and passed to `osascript` on stdin, never through a shell. macOS still asks
    once per app it controls (Automation).
  - Limit: a script that only uses allowed app commands can still do something
    you didn't mean, such as sending a message to the wrong person. That is why
    every change shows the script first: read it before you confirm.
- **Delays** ("close it after 10 seconds") are checked when you ask: the delayed
  actions need the level you have then. When they run, they stop if nobody is
  verified any more (the Mac was locked, or you left with the camera on).
- **Known gaps:** see Known limits below.

## Privacy controls

| Part | Detail |
|---|---|
| Levels | ordinary preferences need L1. Security presets and names need L2. Loosening a security preset, allowing the network, and every deletion, export or factory reset need an L3 confirmation (spoken or clicked) |
| Security presets | voice Standard/Strict (cosine 0.50/0.58, Strict by default), face match Standard/Strict (0.42/0.50), lock after 8 s / 20 s / 1 min away, random liveness checks every 5–15 or 2–5 min. There are no free-form numbers, so nothing can be set to an unsafe value |
| Factory reset | stops verification, deletes templates and the personal fusion model, empties every table (then `VACUUM`), and deletes the Keychain key, so anything left on disk can't be decrypted |
| Locked out | if no face profile, no usable voice profile and no re-scan window remain, nobody can ever be verified. Only then is a reset allowed without verification: it erases and never reveals |
| Export | JSON written with `0600` permissions to a path you pick in the save dialog, never inside the app's data folder |
| Offline guard | wraps socket connect and DNS lookups for the whole backend process; loopback always passes, and so do the online services you switched on (Gemini with a key saved, Google's APIs once connected). `HF_HUB_OFFLINE` stops libraries from trying to download. Ollama is a separate process, so its connections are observed and shown, not blocked |
| Modes | Fast: the fast-mode model (default `qwen2.5:3b`), Whisper small, 4 face checks/s, no memory suggestions. Balanced: your model, Whisper small, 6/s. Quality: your model, Whisper medium, 8/s. A model that isn't downloaded is reported, not faked |

## First-run model download

Models are not shipped inside the app. The first-run download is the only time
the backend reaches the internet, and only because you pressed Download:

- The list of files, their URLs and SHA-256 pins is fixed inside the app
  (`backend/jarvis/model_manifest.json`); the endpoint accepts pack names, never URLs.
- The offline guard opens only GitHub and Hugging Face, only for the download
  thread, only while it runs. Redirects to any other host are refused.
- Every file is hashed and compared with its pin before it is moved into place;
  a mismatch deletes it. Release builds refuse to build with an unpinned file
  (`scripts/pin_models.py --check`).
- Archives are unpacked by listed file name only, so an archive can't write
  outside the models folder.
- Ollama downloads the language models itself, as a separate process; its
  connections are shown in the Privacy view.

## Known limits

- **Voice replay and cloning (the main limit of voice-only):** the voiceprint
  can't tell your live voice from a good recording or a cloned voice. Someone
  with a recording of you, near your Mac, could read your mail summaries or
  calendar. Sending mail and deleting still need an explicit yes, which a
  recording would have to contain too. If that risk matters to you, turn on
  face sign-in (`JARVIS_FACE_AUTH=1`) or keep the Mac locked when you're away.
- **Voice thresholds are untested on your voice until you measure them:** run
  `scripts/eval_voice.py` with your recordings and other voices to see the
  false-accept and false-reject rates at Standard and Strict.
- **Masks and deepfakes (face sign-in only):** the passive anti-spoof model is a small CNN. A
  high-quality 3D mask or a real-time deepfake fed into a virtual camera is
  beyond what it and the challenges can guarantee.
- **Look-alikes and twins (face sign-in only):** face thresholds are tuned for general use; a close
  relative may score near the threshold. The Strict preset raises it.
- **No interruption while speaking:** the microphone is muted while the
  assistant talks.
- **Signing:** releases are signed ad hoc, not notarized by Apple, so macOS
  can't vouch for who built the app. Check the published SHA-256 of the .dmg,
  or build it yourself with `scripts/build_dmg.sh`.
- **Fusion numbers are simulated (face sign-in only):** the classifier's
  accuracy figures come from simulated sessions, not field trials.
- **Free-tier AI:** requests to Gemini's free tier may be read by Google (see
  above), and the free limits can run out; everyday commands keep working.

## Reporting a problem

Open an issue on GitHub, or for anything sensitive contact the maintainer
privately through their GitHub profile before publishing details.

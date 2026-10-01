# Architecture

```
┌──────────────────────── JARVIS.app ────────────────────────┐
│ Tauri 2 shell (Rust)            React + TypeScript UI       │
│  starts the backend with a      command center, setup,      │
│  random port + launch token     settings, first-run         │
│            │   REST + WebSocket on 127.0.0.1 (token)        │
│            ▼                                                │
│ Python backend (FastAPI)  — Contents/Resources/backend/     │
│  mic ─► VAD ─► Whisper ─► wake word / conversation window   │
│              └► ECAPA voiceprint (every addressed utterance)│
│      ─► fast path (patterns) ─┬─► tool runner ─► Kokoro     │
│                               └─► Gemini (command model)    │
│  tools: Mac control · files · notes · calendar · Gmail ·    │
│         Drive · Google Calendar (writing model for mail)    │
│  encrypted SQLite + Keychain key · offline guard            │
└─────────────────────────────────────────────────────────────┘
        │ HTTPS, only the services switched on
        ▼
  generativelanguage.googleapis.com · gmail / www.googleapis.com
  (optional instead: Ollama on 127.0.0.1 · optional: camera + face models)
```

## Processes

- **Shell** (`app/src-tauri/src/lib.rs`): picks a free loopback port and a
  random token, starts the backend (the bundled PyInstaller folder in a
  packaged app, `backend/.venv` in development), hands both to the UI, and on
  quit asks the backend to stop (SIGTERM, then kill after 4 s). The backend also
  exits by itself if the shell disappears.
- **Backend** (`backend/jarvis`): owns the microphone (and the camera, when
  face sign-in is on), so the UI can't inject audio or frames. Everything that
  decides or stores lives here, including the Gemini key and the Google tokens.
- **UI** (`app/src`): a view of backend state over one WebSocket plus REST
  calls. It holds no secrets and makes no security decisions.
- **The brain**: Google's Gemini API (free tier) by default: a fast model for
  commands (`gemma-4-26b-a4b-it`) and a writing model for mail and documents
  (`gemini-3.5-flash-lite`), each the other's fallback when its free quota runs
  out (`llm/gemini.py`, `brain.py`). Opt-in instead: **Claude on Amazon
  Bedrock** (paid by the owner's AWS account; any Claude model per tier, Haiku
  for commands and Sonnet for writing by default, Gemini answering when Bedrock
  can't; `llm/bedrock.py`), or **Ollama** with a local model, started by JARVIS
  if needed and stopped only if it started it.

## Backend layout

```
backend/jarvis/
  __main__.py          entry: loopback-only, offline guard, permission prompts, uvicorn
  api/app.py           REST + WebSocket routes, token and Host/Origin checks, level checks
  service.py           levels, commands, confirmations (and, with face sign-in, camera → face → liveness)
  voice_service.py     mic → VAD → speaker embedding → enrollment / verification → speech
  speech_service.py    transcription, wake word, conversation window, per-utterance voice gate, replies
  google/              OAuth sign-in (loopback + PKCE), Gmail, Drive and Google Calendar REST clients
  auth/face|voice|liveness|fusion, auth/levels.py   the models and the level rules
  brain.py, llm/       fast path (patterns), Gemini client, Ollama client/server/setup, intent schema and prompt
  tools/               runner (the tool registry with a level per tool), scheduler, apps, files, Mac control,
                       Apple bridge, math, google.py (mail / Drive tools, draft read-back)
  memory/              encrypted facts and history, embeddings, suggestions
  security/, database/ AES-256-GCM sealing with the Keychain key, SQLite
  downloads.py         first-run model download (manifest in model_manifest.json)
  netguard.py          offline guard; privacy.py, prefs.py, perf.py, health.py
```

## A command, end to end

1. The mic stream is split into utterances by Silero VAD.
2. Whisper transcribes it. Speech that doesn't start with the assistant's name
   is dropped here (never shown, logged or stored), unless the conversation
   window is open: for a minute after each reply (Settings), follow-ups need no
   name. The name alone plays the pre-synthesised "Yes boss, how may I help
   you?" and opens the window.
3. Only now, for addressed speech, the ECAPA model embeds the clip and compares
   it with the voiceprint (a clip under 0.8 s of speech is too short to judge and
   counts as unclear). Rejected: nothing runs. Unclear: only the harmless
   everyday commands. Recognised: the command goes on, with its verdict.
4. The fast path matches common commands with patterns in under a millisecond
   (no AI request); anything else goes to the command model, which returns JSON
   intents from the tool catalogue. Recent exchanges, with what each one did,
   travel with the request so "him" and "send it" resolve.
5. For each intent the runner looks up the tool's level and recomputes the live
   level: anything beyond level 1 needs this utterance's verdict to be
   "recognised". It runs, plans a confirmation (level 3: deleting, sending a
   mail), or refuses and logs it.
6. The reply is composed by code from what actually happened (a mail summary or
   a draft is written by the writing model) and spoken by Kokoro; the next
   sentence is synthesised while the current one plays.

## Tools

Every command goes through the tool registry (`tools/runner.py`) with a fixed
level. There is no shell from model text: apps are opened by bundle path,
AppleScript gets values as `argv`, and file tools only touch allowed folders.

| Tool | How | Level* |
|---|---|---|
| alarm.set / timer.set / alarm.cancel | encrypted SQLite + in-app scheduler (rings only while JARVIS runs; alarms > 10 min overdue at startup are marked missed) | 2 |
| calendar.create / list | encrypted local events; with Apple sync on, also created in the chosen Apple calendar and listed from all Apple calendars | 2 / 1 |
| notes.add / search | encrypted local notes (fuzzy search); with sync on, also in Apple Notes folder "JARVIS" | 2 / 1 |
| app.open | any `.app` in the standard Applications folders, fuzzy and Devanagari-aware name match, opened with `open <bundle>` (no shell, no arguments) | 2 |
| files.search | `mdfind -onlyin` per allowed folder; file names only; system, library and hidden folders refused | 1 |
| files.recent | newest files in the allowed folders by kind, day and folder ("the PDF I downloaded yesterday"); names and dates only, hidden folders and app bundles skipped | 1 |
| files.reveal | shows the newest match, or the file just found, in Finder (`open -R`) | 2 |
| files.trash | one plain file in an allowed folder (no folders, apps, symlinks or hidden files), named in the confirmation, re-checked after it, then moved to the Trash the way Finder does (Put Back works) | 3 |
| app.close | quits politely through AppKit (like ⌘Q, so apps can ask to save) | 2 |
| folder.open / web.open | a known or allowed folder in Finder; a URL or a search in the default browser | 2 |
| system.volume / media.control / system.battery / system.lock / alarm.list | volume and mute, play/pause/next on Spotify or Music, battery level, lock screen, list alarms | 2 / 1 |
| screen.shot / display.brightness / display.dark_mode / settings.open | screenshot to the Desktop, brightness, dark mode through System Events, a named System Settings pane | 2 |
| clipboard.read / clipboard.note / text.type | read the clipboard, save it as a note, type dictated text into the front app | 1 / 2 / 2 |
| ai.ask | opens Claude or ChatGPT (app, else the website) with the dictated prompt written in, never sent | 2 |
| memory.remember / memory.forget / history.search | encrypted facts and history (see ML.md) | 2 / 3 / 1 |
| notes.delete / calendar.delete | resolved to one exact item first (fuzzy match), shown in the confirmation, deleted only after it; copies in Apple's apps are left alone | 3 |
| email.unread / email.summary / email.read | Gmail REST: ids, then headers + snippets (summary: one writing-model request) or one full message (text/plain, quoted chain dropped) | 2 |
| email.draft | recipient by name from past mail headers (or "him" = the mail just read), subject + body from the writing model, saved as a Gmail draft (in the thread when replying) | 2 |
| email.send | writes or reuses the latest draft, reads it back in the confirmation, `drafts/send` only after it | 3 |
| drive.search / recent / summarize / open | Drive REST (read-only): name, then full-text search; Docs/Sheets/Slides exported as text for a summary; `webViewLink` opened in the browser | 2 |
| calendar.* with Google | when Google Calendar is connected, new events are also created in the primary calendar and listed from it | as above |

\*See [SECURITY.md](SECURITY.md#levels). Level-3 tools cannot be run
directly: the runner only plans them, and deletion happens after confirmation.
Model output is untrusted: each tool validates its arguments (times in the
future, sane timer lengths, known apps, allowed folders). AppleScript
receives values as `argv`, never as script text.

## First run and packaging

- On first launch the backend reports `models_needed`; the UI shows the
  download screen before setup. Voice-only installs skip the face and
  anti-spoof packs. `downloads.py` fetches the packs in
  `model_manifest.json` (resumable `.part` files, SHA-256 checks, a disk-space
  check first), and `llm/setup.py` detects Ollama and pulls its models. Then
  the backend restarts itself in place (same port and token) to load them.
- `scripts/build_dmg.sh` pins the model checksums, builds the backend with
  PyInstaller (`backend/sidecar/jarvis-backend.spec`, a folder rather than one
  file so launches don't unpack gigabytes), builds the Tauri app, copies the
  backend into `Contents/Resources/backend`, signs everything ad hoc with the
  hardened runtime and `app/src-tauri/Entitlements.plist` (camera, microphone,
  Apple Events), and makes the .dmg. A version tag runs the same script on a
  macOS runner and drafts a GitHub release (`.github/workflows/release.yml`).
- Data lives in `~/Library/Application Support/JarvisAssistant` (models in its
  `models/` folder for the packaged app, `<repo>/models` in development).

## Tests

- `backend/`: `pytest` (fakes for camera, mic, models and the LLM; Mac-only
  tests are marked and skipped elsewhere), `ruff`, `mypy`.
- `app/`: Vitest + Testing Library against a scripted fake backend, ESLint, tsc.
- `app/src-tauri/`: `cargo fmt`, `cargo clippy`.
- CI runs all of it on every push (`.github/workflows/ci.yml`).

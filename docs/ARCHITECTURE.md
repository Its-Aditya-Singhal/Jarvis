# Architecture

```
┌──────────────────────── JARVIS.app ────────────────────────┐
│ Tauri 2 shell (Rust)            React + TypeScript UI       │
│  starts the backend with a      command center, setup,      │
│  random port + launch token     first-run downloads         │
│            │   REST + WebSocket on 127.0.0.1 (token)        │
│            ▼                                                │
│ Python backend (FastAPI)  — Contents/Resources/backend/     │
│  camera ─► face engine ─► continuous face auth ─┐           │
│            liveness gate (anti-spoof + challenges)├► levels  │
│  mic ─► VAD ─► ECAPA voiceprint ─────────────────┘ + fusion │
│      └► Whisper ─► wake word ─► fast path / Ollama intents  │
│                                   ─► tool runner ─► Kokoro  │
│  encrypted SQLite + Keychain key · offline guard            │
└─────────────────────────────────────────────────────────────┘
              │ loopback only
              ▼
      Ollama (separate process): qwen2.5, bge-m3
```

## Processes

- **Shell** (`app/src-tauri/src/lib.rs`): picks a free loopback port and a
  random token, starts the backend (the bundled PyInstaller folder in a
  packaged app, `backend/.venv` in development), hands both to the UI, and on
  quit asks the backend to stop (SIGTERM, then kill after 4 s). The backend also
  exits by itself if the shell disappears.
- **Backend** (`backend/jarvis`): owns the camera and microphone, so the UI
  can't inject frames or audio. Everything that decides or stores lives here.
- **UI** (`app/src`): a view of backend state over one WebSocket plus REST
  calls. It holds no secrets and makes no security decisions.
- **Ollama**: runs the language and embedding models. JARVIS starts it if it
  isn't running (models in `~/Developer/ollama/models` by default) and stops
  only a server it started.

## Backend layout

```
backend/jarvis/
  __main__.py          entry: loopback-only, offline guard, permission prompts, uvicorn
  api/app.py           REST + WebSocket routes, token and Host/Origin checks, level checks
  service.py           the core loop: camera → face → liveness → levels, commands, confirmations
  voice_service.py     mic → VAD → speaker embedding → enrollment / verification → speech
  speech_service.py    transcription, wake word, owner checks, spoken replies
  auth/face|voice|liveness|fusion, auth/levels.py   the models and the level rules
  brain.py, llm/       fast path (patterns), Ollama client/server/setup, intent schema and prompt
  tools/               runner (the tool registry with a level per tool), scheduler, apps, files, Mac control, Apple bridge, math
  memory/              encrypted facts and history, embeddings, suggestions
  security/, database/ AES-256-GCM sealing with the Keychain key, SQLite
  downloads.py         first-run model download (manifest in model_manifest.json)
  netguard.py          offline guard; privacy.py, prefs.py, perf.py, health.py
```

## A command, end to end

1. The mic stream is split into utterances by Silero VAD; each one is embedded
   (ECAPA) and compared with your voiceprint.
2. Whisper transcribes it. Speech that doesn't start with the assistant's name
   is dropped here: never shown, logged or stored.
3. The fast path matches common commands with patterns in under a millisecond;
   anything else goes to the local model, which returns JSON intents.
4. For each intent the runner looks up the tool's level, recomputes the live
   level from face, liveness and voice evidence, and runs, plans a
   confirmation (level 3), or refuses and logs it.
5. The reply is composed by code from what actually happened and spoken by
   Kokoro; the next sentence is synthesised while the current one plays.

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

\*See [SECURITY.md](SECURITY.md#levels). Level-3 tools cannot be run
directly: the runner only plans them, and deletion happens after confirmation.
Model output is untrusted: each tool validates its arguments (times in the
future, sane timer lengths, known apps, allowed folders). AppleScript
receives values as `argv`, never as script text.

## First run and packaging

- On first launch the backend reports `models_needed`; the UI shows the
  download screen before setup. `downloads.py` fetches the packs in
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

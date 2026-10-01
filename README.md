# JARVIS

**A light, voice-only assistant for the Mac that answers only to your voice.**

JARVIS listens for its name (you choose it: JARVIS, FRIDAY, anything) and does
things by voice, in English, Hindi or Hinglish: everyday Mac control, your
Gmail, Google Drive and Google Calendar, and anything else you ask in your own
words. Its brain is Google's Gemini API on the free tier, so nothing heavy runs
on the Mac and it can stay on all day. Every utterance is checked against your
voiceprint before it reads or does anything.

[Website](https://its-aditya-singhal.github.io/Jarvis/) ·
[Download](https://github.com/Its-Aditya-Singhal/Jarvis/releases/latest) ·
[User guide](docs/USER_GUIDE.md) · [Security](docs/SECURITY.md) ·
[How the models work](docs/ML.md) · [Architecture](docs/ARCHITECTURE.md) ·
[Changelog](CHANGELOG.md)

## What it does

- **Answers only to you.** A long voice enrollment (24 phrases: normal, soft,
  loud, from a step back) builds an ECAPA voiceprint. Every request is judged on
  its own words, follow-ups included: anything that reads your data or acts
  needs that very utterance to be your voice; a doubtful voice only gets
  harmless commands like "louder"; another voice gets nothing. No camera is
  used (face sign-in is still available as an option).
- **Everyday things, instantly.** Volume, brightness, dark mode, screenshots,
  timers, alarms, the stopwatch, music, web and YouTube search, opening and
  quitting apps, folders, notes, calendar, recent files, the clipboard, math
  and conversions. The common phrasings are understood by a pattern matcher in
  under a millisecond, with no AI request at all.
- **Your Google account.** "Summarize my last 10 emails", "any new mail?",
  "what did Rahul mail me?", "send him a mail confirming I'll come", "find the
  budget sheet in my Drive", "what's on my calendar tomorrow?". A mail is
  always read back to you and sent only after your "yes" (in your verified
  voice) or a click.
- **Not a fixed command list.** Anything else goes to the AI with the full tool
  catalogue, and for Mac apps no tool covers it writes an AppleScript (shown to
  you before it changes anything).
- **Conversation.** Say "Jarvis" and it answers at once ("Yes boss, how may I
  help you?", pre-recorded). After each reply it keeps listening for a minute
  without the wake word and remembers what was just said, so follow-ups work.
- **Light.** No local language model by default, no camera, no OpenCV loaded,
  resource sampling once a minute while no window is open, and a plain UI with
  no animation loops.
- **Private where it counts.** Whisper speech recognition and the Kokoro voice
  run on the Mac; speech that doesn't start with the assistant's name is thrown
  away unheard. Your keys and tokens are sealed with a Keychain key. Offline
  mode lets only the services you switched on through.

## Install

Requires macOS 14 or later on Apple Silicon and about 2 GB of free space.
Download the .dmg from the
[latest release](https://github.com/Its-Aditya-Singhal/Jarvis/releases/latest),
drag JARVIS to Applications, then open it and allow it once in System Settings
→ Privacy & Security (it is signed but not notarized). The
[user guide](docs/USER_GUIDE.md) walks through the first run, the free Gemini
key, connecting Google and the voice enrollment.

## Developer setup

Requirements: macOS on Apple Silicon, Python 3.12, Node 20+, Rust (`brew install rustup && rustup default stable`).

```bash
cd backend && python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python ../scripts/download_models.py   # speech, voice and VAD models, checksummed
.venv/bin/python -m pytest -m "not ollama"        # the whole suite, no network needed
cd ../app && npm install && npm run tauri dev
```

The Gemini key and the Google OAuth client are entered in the app (Settings →
AI, Settings → Google account), never in files. Useful scripts:

- `scripts/eval_commands.py`: runs the routing corpus through the real command path.
- `scripts/eval_voice.py`: false accepts / false rejects of the voice match from recordings.
- `python -m jarvis.headless`: the whole backend with simulated hardware, for UI work.

Optional local brain: `brew install ollama && ollama pull qwen2.5:7b`, then Settings → AI → Local.
Optional face sign-in: `JARVIS_FACE_AUTH=1` (downloads the face and anti-spoof models).

Build the .dmg on an Apple Silicon Mac with `scripts/build_dmg.sh` (see
[ARCHITECTURE.md](docs/ARCHITECTURE.md#first-run-and-packaging)); pushing a
`v*` tag does the same on GitHub Actions and drafts a release.

## Licence

JARVIS's own code is under the [MIT licence](LICENSE).

The models JARVIS downloads have their own licences: Whisper, Kokoro, ECAPA
(SpeechBrain) and Silero VAD are under permissive licences; the optional face
models (InsightFace) are for non-commercial research use. Gemini and Gemma are
used through Google's API under Google's terms. Check them before using JARVIS
commercially.

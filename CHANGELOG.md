# Changelog

## Unreleased — voice-only, Gemini, Google account

### Added
- **Gmail, Google Drive and Google Calendar.** Settings → Google account walks
  through creating your own free OAuth client, then Connect signs in through the
  browser (loopback redirect, PKCE, checked state). "Summarize my last 10
  emails", "any new mail", "what did Rahul mail me", "draft a mail to…", "send
  him a mail confirming I'll come", "find the budget sheet in my drive",
  "summarise it", "open it". With Google Calendar connected, new events go
  there too and the day's list includes it.
- **Read-back before sending.** A mail is saved as a Gmail draft, read back
  aloud, shown in the confirmation card, and sent only after a "yes" in your
  recognised voice or a click on Send.
- **Gemini brain** (free tier): a fast commands model and a writing model, each
  the other's fallback when its free limit runs out, with a clear message when
  the daily limit is used up. Key and model names in Settings → AI; the key is
  sealed with the Keychain key and never shown again. Ollama stays as an opt-in.
- **Claude on Amazon Bedrock** (optional, paid by your AWS account): a third
  choice in Settings → AI. Any Claude model your account can use, one for
  most requests, mail summaries included (Haiku 4.5 by default), and one for
  harder ones (Sonnet 5 by default: drafting mails, writing, planning, long
  threads, multi-step requests; JARVIS picks per request), plus the AWS region. Uses a Bedrock API key, sealed like the Gemini key. When
  Bedrock can't answer (throttled, model access not turned on, key expired) and
  a Gemini key is saved, Gemini answers and JARVIS says why. Gemini stays the
  default.
- **Instant greeting:** the name alone answers "Yes boss, how may I help
  you?", synthesised once at startup and kept.
- **Conversation window:** after every reply JARVIS keeps listening without
  its name for 30 s / 1 min (default) / 2 min, and remembers what each command
  did for ten minutes; "thanks" or "that's all" ends it.
- `scripts/eval_voice.py`: false accepts and false rejects of the voice match
  at both presets, from your recordings and other voices.

### Changed
- **Voice-only sign-in by default:** no camera, face model, anti-spoof model or
  face loop; the camera isn't even opened for a permission prompt.
  `JARVIS_FACE_AUTH=1` brings face sign-in back.
- **Stronger voice check:** every addressed utterance is judged, follow-ups
  and short ones included; anything that reads or acts needs that very
  utterance to be your voice; an unclear voice only gets harmless everyday
  commands; another voice gets nothing (everyday commands used to run for any
  voice). Strict is the default preset; enrollment is 24 phrases (normal, soft,
  louder, from a step back).
- Typed commands are off by default (the keyboard would bypass the voice);
  "Voice only" now also keeps typing to the harmless everyday commands.
- **Lighter:** OpenCV isn't loaded without face sign-in; resource sampling
  drops to once a minute while no window is open and never asks Ollama with
  the Gemini brain; memory recall matches words by default.
- **A cleaner interface:** a calm dark theme with the system font, no grid,
  glows or glitch effects, and a simple status ring instead of the particle
  orb that redrew every frame. Settings hides what doesn't apply.

## 1.0.0 — unreleased

The first release: a packaged Mac app with a first-run download, a website and
full documentation.

### Fixed
- On 8 GB Macs the language model's context (2K tokens) was smaller than the
  command prompt (~3K), so Ollama cut off the prompt's rules and tool list.
  Small Macs now use 5K, others 6K.
- A number in a delay was taken as a volume level ("close it after 10 seconds"
  set the volume to 10%). Volume or brightness actions the request never
  mentions are now dropped.

### Added
- **Any command**: anything no built-in tool covers ("add milk to my Reminders",
  "play my workout playlist", "what's the title of this Safari tab") goes to the
  local model, which writes an AppleScript for it. Scripts that only read run
  straight away; anything that changes something shows the full script and waits
  for your "yes". Shell commands, Terminal, other scripts, deleting or writing
  files, passwords and admin rights are never run. On 8 GB Macs every script
  asks first. Settings → Security → "Anything else": Ask before changes, Always
  ask, or Off.
- **Delays**: "open WhatsApp and close it after 10 seconds", "in 5 minutes open
  Safari", "10 second baad band karo". Waiting actions count down on the main
  screen and can be cancelled.
- Running timers count down on the main screen, not only under Tools.
- "close it" refers to the app just opened; lists of apps ("open Notes,
  Calendar and Safari"); full / half / max volume and brightness; "Jarvis, …"
  before a command.
- **First-run model download**: the app lists the models it needs, downloads
  them (resumable, every file checked against its SHA-256, a disk-space check
  first), detects Ollama with install guidance, pulls the language models with
  progress, then restarts into setup. Settings → On-device models adds optional
  ones such as Whisper medium.
- **Packaging**: the backend ships inside the app as a PyInstaller folder;
  `scripts/build_dmg.sh` builds, signs (ad hoc, hardened runtime, camera /
  microphone / Apple Events entitlements) and makes the .dmg. Tags build it on
  GitHub Actions.
- **Website** on GitHub Pages, with docs and privacy pages.
- **Docs**: user guide, security (threat model and limits), how the models
  work, architecture.
- **App polish**: ⌘K command palette, keyboard shortcuts, an About view,
  clearer loading and error states, accessibility fixes.
- Files, deeper: recent and dated files ("the PDF I downloaded yesterday"),
  Show in Finder, Move to Trash (level 3).
- Ask Claude or ChatGPT: opens a new chat with the dictated prompt written in,
  never sent for you.
- Screen and clipboard: screenshot, brightness, dark mode, System Settings
  panes, read the clipboard, save it as a note, type dictated text.
- Math and conversions answered instantly, without the model.
- Microphone diagnostics that name the real cause and the fix, with a mic
  picker and retry.

### Changed
- The packaged app keeps its models in `~/Library/Application Support/JarvisAssistant/models`
  and logs to its `logs/` folder.
- `scripts/download_models.py` uses the same checksummed manifest as the app.

### Before 1.0
Phases 1–9 built the foundation: face identity, voice verification, liveness,
speech in and out, the local language model, tools, auth levels with a trained
fusion model, memory, and the privacy dashboard, settings and performance modes.

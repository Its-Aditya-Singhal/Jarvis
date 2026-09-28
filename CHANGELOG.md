# Changelog

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

# Mac test checklist (before tagging v1.0.0)

Everything the cloud session couldn't test: it needs the real Mac, camera,
microphone, Apple GPU and a signed app. Tick each box; anything that fails is a
bug to fix before the tag.

## 0. Pull and run the checks

```bash
git pull
cd backend && .venv/bin/pip install -e ".[dev]" && .venv/bin/python -m pytest -q   # Mac tests run here too
cd ../app && npm ci && npm test && npm run lint
cd src-tauri && cargo clippy --all-targets -- -D warnings
```

- [ ] All green (the Mac-only tests run on macOS: `say` voices, AppleScript, MLX)

## 1. Pin the model checksums

```bash
backend/.venv/bin/python scripts/pin_models.py      # hashes your models/ copies, else downloads
backend/.venv/bin/python scripts/pin_models.py --check
git commit -am "Pin model checksums" && git push
```

- [ ] Every file pinned (Hugging Face files had no pin from the cloud session;
      GitHub-hosted ones were pinned and verified there)

## 2. Build the .dmg

```bash
scripts/build_dmg.sh
```

- [ ] Builds without errors; `codesign --verify` passes; note the .dmg size
- [ ] `dist/JARVIS-1.0.0-arm64.dmg` opens and shows JARVIS + Applications
- [ ] If PyInstaller misses a module at runtime, the backend log
      (`~/Library/Application Support/JarvisAssistant/logs/backend.log`) names it:
      add it to `PACKAGES` in `backend/sidecar/jarvis-backend.spec`
- [ ] If built on macOS 15, check the app still opens on macOS 14 (MLX wheels
      can require the build machine's version); otherwise raise
      `minimumSystemVersion` in `tauri.conf.json`

## 3. Fresh install (a new macOS user account is best)

- [ ] Gatekeeper: first open is blocked; System Settings → Privacy & Security →
      Open Anyway works
- [ ] Camera and microphone prompts name **JARVIS** and show the usage text
- [ ] A Keychain prompt ("jarvis-backend wants to access key jarvis-assistant")
      appears only if an earlier build made the key; after Always Allow it
      doesn't come back until the next rebuild or update
- [ ] The first screen is the model download: sizes and free space look right
- [ ] Download runs with progress, speed and time left; the MLX pack appears
- [ ] Pause, then Resume: continues from where it stopped (not from zero)
- [ ] Turn Wi-Fi off mid-download: a clear error, Resume works after Wi-Fi is back
- [ ] Ollama not installed: Get Ollama opens the browser; after installing,
      Check again finds it; Start Ollama works if it isn't running
- [ ] Pulling qwen2.5:7b shows progress; Continue restarts into setup
- [ ] Privacy view afterwards: offline mode on, the download shows as allowed
      "model download" attempts only

## 4. Microphone (open question from phase A.3)

- [ ] Started from Finder (not Terminal), Settings → Microphone shows the right
      cause and app name for: permission denied, no input device, wrong device
- [ ] Retry and the mic picker work

## 5. Setup and identity

- [ ] Names, voice pick, face scan (each pose checked), 24 voice phrases
- [ ] Unlock: face → liveness challenge → L1; speak → L2
- [ ] A photo or phone screen of you: SPOOF DETECTED, logged
- [ ] Someone else in view: capped at L1; a stranger alone: AUTHENTICATION DENIED
- [ ] Face once (default): after the unlock the camera light goes off and the
      preview says FACE VERIFIED · CAMERA OFF; voice commands still work
- [ ] Settings → Camera → Face all the time: the camera comes back on
- [ ] A typed command ("set a timer for 2 minutes") runs without speaking; with
      Settings → Commands → Voice only it asks you to say it instead
- [ ] Walk away (Face all the time): locks after the chosen time; come back: unlocks
- [ ] Wake word with your chosen name, in English and Hindi

## 6. Every command (say each once)

- [ ] Timer, alarm (rings, snooze, "stop"), cancel alarm, "what alarms do I have"
- [ ] Calendar create/list, notes add/search/delete (confirm), Apple sync on and off
- [ ] Open/quit apps (incl. Hindi), open folders, websites, Google search
- [ ] Volume, mute, music controls, battery, lock screen
- [ ] Screenshot (Screen Recording prompt), brightness, dark mode (Automation
      prompt), a System Settings pane (lands on the right one)
- [ ] Clipboard read / save as note, type dictated text (Accessibility prompt)
- [ ] "the PDF I downloaded yesterday" finds it; Show in Finder; Move to Trash
      asks to confirm, then Put Back works in Finder
- [ ] Ask Claude: the app gets ⌘N and the pasted prompt, not sent; without the
      app claude.ai opens pre-filled; ChatGPT: prompt on the clipboard
- [ ] Math and conversions, "days till …", time and date: instant
- [ ] Remember / recall in three languages; forget needs a confirmation
- [ ] A question to the model; compound request ("wake me at 7 and note to buy milk")
- [ ] "open WhatsApp and close it after 10 seconds": opens, the countdown shows on
      the main screen, it closes 10 s later; ✕ on a countdown cancels it
- [ ] "set a timer for 1 minute": the countdown shows on the main screen
- [ ] Any command: "add milk to my Reminders" shows the script and waits; Confirm
      adds it (Automation prompt for Reminders). "what song is playing" answers
      without asking (16 GB) or asks first (8 GB). "empty the trash" and "run
      rm -rf" are refused
- [ ] `backend/.venv/bin/python scripts/eval_commands.py --scripts` with Ollama
      running: pass rate on qwen2.5:7b and qwen2.5:3b, scripts compile

## 7. App polish

- [ ] ⌘K opens the palette; arrows + Enter run an entry; Esc closes it
- [ ] ⌘1…⌘7 switch views, ⌘, opens Settings, `/` focuses the command bar
- [ ] About view shows the version, models and links (links open the browser)
- [ ] VoiceOver reads the navigation, status bar and buttons sensibly
- [ ] With Reduce Motion on, the core and progress bars stop animating

## 8. Privacy, settings, performance

- [ ] Privacy: sizes, export (file is 0600), delete each kind, factory reset
- [ ] Settings: every card; performance modes Fast/Balanced/Quality/Auto on
      battery and on power; Whisper medium download from On-device models, then
      Quality mode uses it after Restart
- [ ] Memory at idle and after an hour (Activity Monitor): note JARVIS, the
      backend and Ollama
- [ ] Quit (⌘Q): camera light goes off, backend and Ollama (if JARVIS started it) exit

## 9. Release

- [ ] Enable GitHub Pages: repository Settings → Pages → Source: GitHub Actions;
      the Website workflow publishes `website/`
- [ ] Decide the licence for the code (there's no LICENSE file yet)
- [ ] Update the CHANGELOG date, tag `v1.0.0`, push the tag; the Release
      workflow drafts the release with the .dmg and SHA256SUMS.txt; publish it

## Voice-only, Gemini and Google (October 2026)

Only Aditya can do these: they need his voice, his Google account and his keys.
Never paste a key or a client secret into a chat.

- [ ] `git pull`; old local edits in `~/Developer/jarvis` stashed or discarded first; `scripts/build_dmg.sh`
- [ ] Voice-only: the camera light never comes on, from launch to quit
- [ ] Settings → AI: paste the Gemini key from https://aistudio.google.com/apikey, Save, **Test** says both models answered
- [ ] "Jarvis" alone → "Yes boss, how may I help you?" plays at once (no pause before it)
- [ ] A command the patterns don't know ("could you get Notes up for me") → one Gemini request; note the latency for Gemma 4 vs Flash-Lite.
      If Gemma 4 is slow or returns broken JSON, set the commands model to `gemini-3.5-flash-lite` too
- [ ] Optional, paid (AWS credits): Settings → AI → Claude on Bedrock. In the AWS console first: Bedrock → Model access, turn on
      Claude Haiku 4.5 and Claude Sonnet 5 (or the models you pick); Bedrock → API keys, create a **long-term** key
      (short-term keys stop after 12 hours). Paste it, keep region `us-east-1` (or yours), **Test** says both models answered.
      Ask a non-everyday question; note the latency. Check AWS Billing → Credits that the credit covers Anthropic models.
- [ ] Settings → Google account: create the OAuth client (Desktop app, consent screen published), paste ID + secret, Connect → "Connected · your address"
- [ ] "summarize my last 10 emails" · "any new mail" · "what did <a friend> mail me" → "okay send him a mail confirming my presence"
      → it reads the draft back and waits; "yes, send it" sends it; check it in Gmail → Sent (send it to yourself first)
- [ ] "find <a file> in my drive" → "summarise it" → "open it"; "what's on my calendar tomorrow" includes Google Calendar events
- [ ] Follow-up window: after a reply, a follow-up without the name works for about a minute; "thanks" ends it; someone else talking closes it
- [ ] Voice enrollment (24 phrases); afterwards another person saying "Jarvis, read my email" is refused, and so is "Jarvis, volume up"
- [ ] Record ~20 short clips of yourself on another day plus clips of other people, run
      `backend/.venv/bin/python scripts/eval_voice.py --enroll … --owner … --others …`, note FAR / FRR at Standard and Strict
- [ ] Idle for 10 minutes with the window closed: Activity Monitor shows the backend's CPU % (should be ~0–1 %), memory and Energy Impact

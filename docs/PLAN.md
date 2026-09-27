# Road to 1.0 — work plan

Work happens in two places: a cloud session (Linux, no camera/mic/Apple GPU) does
everything that doesn't need the Mac, on the `cloud-1.0` branch; then we return to
the Mac to test the real app, fix what we find, build the `.dmg` and release.

Decisions: public GitHub repo · website on GitHub Pages, `.dmg` on GitHub Releases ·
models downloaded on first run · app keeps its command-center look (polish only) ·
the website gets its own design, related to the app but not a copy.

## A. Cloud

### 1. Test & debug infrastructure
- [x] Python suite runs on Linux; Mac/hardware tests marked (`@pytest.mark.mac`) and skipped, not deleted
- [x] Fakes for camera, microphone, STT/TTS, face/voice models and the LLM so the service runs end to end headless
- [x] UI tests (Vitest + Testing Library): setup wizard, settings, privacy, confirm card, status bar
- [x] Linters/type checks: ruff + mypy/pyright (backend), ESLint + tsc (UI), cargo clippy (shell)
- [x] Command-routing corpus: hundreds of EN / HI / Hinglish phrasings → expected tool + args (fast path and LLM prompt)
- [x] GitHub Actions: tests + lint on every push (Linux); `.dmg` build on tags (macOS runner)

### 2. Full code review, module by module (each bug → test + own commit)
- [ ] Security: auth level on every tool path, launch token on every REST/WS route, path tricks in file/folder tools, AppleScript/argv safety, offline-guard bypasses, pending-confirmation replay
- [ ] Stability: thread safety (camera / mic / speech / brain), leaked threads and processes, error handling, long-run memory growth
- [ ] Performance: startup time, per-command latency, idle CPU

### 3. Fixes and new commands
- [ ] Microphone: report the real cause (permission denied / no input device / busy / device vanished), mic picker + retry in Settings, cause-specific fix text, Info.plist usage strings checked
- [ ] Math & conversions: arithmetic, percentages, units, currency-free date math ("days till …") — instant, no model
- [ ] Screen & display: screenshot, brightness, dark mode, open a System Settings page
- [ ] Clipboard & text: read clipboard, save clipboard as a note, type/paste dictated text into the front app
- [ ] Files, deeper: recent/dated files ("the PDF I downloaded yesterday"), reveal in Finder, move to Trash (level 3)
- [ ] Ask Claude: "open Claude and ask it to build a website for …" → opens Claude (app, else claude.ai) with the dictated prompt pre-filled; shows the prompt before sending; same for ChatGPT

### 4. Phase 10
- [ ] App polish: spacing/type/motion consistency, empty/loading/error states, ⌘K command palette, keyboard shortcuts, About page, accessibility pass
- [ ] First-run model download screen: progress, resume, checksums, disk-space check, Ollama detection + install guidance
- [ ] Packaging: PyInstaller backend sidecar (arm64), Tauri sidecar config, entitlements (camera, mic, Apple Events), ad-hoc signing, `scripts/build_dmg.sh`
- [ ] Website (own design): animated landing page (scroll-driven motion, transitions, hover effects), features, how the security works, download, docs, privacy
- [ ] Docs: README, ARCHITECTURE, SECURITY (threat model + limits), ML (face/voice/liveness/fusion with measured numbers), USER GUIDE, CHANGELOG, version 1.0.0
- [ ] Mac test checklist for section B

## B. Back on the Mac
- [ ] Microphone fixed and verified with the new diagnostics
- [ ] Build the `.dmg`; install on a fresh macOS user account (first-run download, permissions)
- [ ] Walk the test checklist: setup, face / voice / liveness, wake word, every command, privacy, settings, performance modes, memory at idle and after an hour, clean quit
- [ ] Fix what we find, merge `cloud-1.0` → `main`, tag v1.0.0, publish the release and website

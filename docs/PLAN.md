# Road to 1.0 — work plan

Work happens in two places: a cloud session (Linux, no camera/mic/Apple GPU) does
everything that doesn't need the Mac, on the `cloud-1.0` branch; then we return to
the Mac to test the real app, fix what we find, build the `.dmg` and release.

Decisions: public GitHub repo · website on GitHub Pages, `.dmg` on GitHub Releases ·
models downloaded on first run · app keeps its command-center look (polish only) ·
the website gets its own design, related to the app but not a copy.

## Where to pick up
- Work on `main`. Section A is done. Next: **B, on the Mac**, following `docs/MAC_TEST_CHECKLIST.md` (pin model checksums, `scripts/build_dmg.sh`, fresh-install test, every command), then tag v1.0.0.
- 2026-09-28 (cloud): any command (generated AppleScript, the owner's option 1), delays ("close it after 10 seconds"), timer countdown on the main screen and the 8 GB context fix are on `main` with tests, but not yet built or run on a Mac. Next on the Mac: `scripts/eval_commands.py --scripts` on 7B and 3B, rebuild the .dmg, and the new lines in checklist section 6.
- Open question from the owner: what Settings → Microphone says when JARVIS is started the usual way (checklist section 4).
- Owner decisions before the tag: a licence for the code (no LICENSE file yet), and enabling GitHub Pages (Settings → Pages → Source: GitHub Actions).
- Packaging was test-built on Linux only: the PyInstaller backend starts, loads the face, liveness and Kokoro models and restarts itself after a download. The macOS build, signing and TCC prompts are untested.
- Rules: no paid APIs or services · no Claude/AI co-author trailers or credits in commits or files · commit after each fix · every command goes through the tool registry with an auth level (no shell from model text; generated AppleScript only through `tools/agent.py`'s checks).
- Checks: `backend/.venv/bin/python -m pytest -q`, `ruff check .`, `mypy` (in `backend/`); `npm test`, `npm run lint` (in `app/`); `cargo clippy` (in `app/src-tauri/`).
- Local run for UI checks: `npm run tauri dev` in `app/` (needs Ollama: `~/Developer/ollama/start.sh`).

## A. Cloud

### 1. Test & debug infrastructure
- [x] Python suite runs on Linux; Mac/hardware tests marked (`@pytest.mark.mac`) and skipped, not deleted
- [x] Fakes for camera, microphone, STT/TTS, face/voice models and the LLM so the service runs end to end headless
- [x] UI tests (Vitest + Testing Library): setup wizard, settings, privacy, confirm card, status bar
- [x] Linters/type checks: ruff + mypy/pyright (backend), ESLint + tsc (UI), cargo clippy (shell)
- [x] Command-routing corpus: hundreds of EN / HI / Hinglish phrasings → expected tool + args (fast path and LLM prompt)
- [x] GitHub Actions: tests + lint on every push (Linux); `.dmg` build on tags (macOS runner)

### 2. Full code review, module by module (each bug → test + own commit)
- [x] Security: auth level on every tool path, launch token on every REST/WS route, path tricks in file/folder tools, AppleScript/argv safety, offline-guard bypasses, pending-confirmation replay
- [x] Stability: thread safety (camera / mic / speech / brain), leaked threads and processes, error handling, long-run memory growth
- [x] Performance: startup time, per-command latency, idle CPU

### 3. Fixes and new commands
- [x] Microphone: report the real cause (permission denied / no input device / busy / device vanished), mic picker + retry in Settings, cause-specific fix text, Info.plist usage strings checked
- [x] Math & conversions: arithmetic, percentages, units, currency-free date math ("days till …") — instant, no model
- [x] Screen & display: screenshot, brightness, dark mode, open a System Settings page
- [x] Clipboard & text: read clipboard, save clipboard as a note, type/paste dictated text into the front app
- [x] Files, deeper: recent/dated files ("the PDF I downloaded yesterday"), reveal in Finder, move to Trash (level 3)
- [x] Ask Claude: "open Claude and ask it to build a website for …" → opens Claude (app, else claude.ai) with the dictated prompt pre-filled; shows the prompt before sending; same for ChatGPT

### 4. Phase 10
- [x] App polish: spacing/type/motion consistency, empty/loading/error states, ⌘K command palette, keyboard shortcuts, About page, accessibility pass
- [x] First-run model download screen: progress, resume, checksums, disk-space check, Ollama detection + install guidance
- [x] Packaging: PyInstaller backend sidecar (arm64), Tauri sidecar config, entitlements (camera, `com.apple.security.device.audio-input`, Apple Events), ad-hoc signing, `scripts/build_dmg.sh`
- [x] Website (own design): animated landing page (scroll-driven motion, transitions, hover effects), features, how the security works, download, docs, privacy
- [x] Docs: README, ARCHITECTURE, SECURITY (threat model + limits), ML (face/voice/liveness/fusion with measured numbers), USER GUIDE, CHANGELOG, version 1.0.0
- [x] Mac test checklist for section B

## B. Back on the Mac
- [ ] Microphone fixed and verified with the new diagnostics
- [ ] Build the `.dmg`; install on a fresh macOS user account (first-run download, permissions)
- [ ] Walk the test checklist: setup, face / voice / liveness, wake word, every command, privacy, settings, performance modes, memory at idle and after an hour, clean quit
- [ ] Fix what we find, merge `cloud-1.0` → `main`, tag v1.0.0, publish the release and website

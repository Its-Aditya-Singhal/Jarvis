# JARVIS: what's left, on the Mac

Written 2026-10-01 for a fresh Claude session **running on Aditya's Mac**, with no memory of earlier chats.
Everything you need is here. The cloud work is finished; what remains needs the real Mac, Aditya's voice,
his Google account and his keys.

Repo: https://github.com/Its-Aditya-Singhal/Jarvis. macOS (Apple Silicon) desktop assistant: Tauri 2 + React/TS
UI in `app/`, Python FastAPI backend in `backend/jarvis/`. Local clone on the Mac: `~/Developer/jarvis`.
Read `docs/ARCHITECTURE.md` for the overview and `docs/USER_GUIDE.md` for how the app is used.

---

## 1. Rules for working in this repo (must follow)

- Push finished, tested work **straight to `main`** (rebase on `origin/main` first). No PRs.
- **Commit after each fix**; small commits with a plain-English message describing what the user sees.
- Commit author: `Aditya Singhal <92255610+Its-Aditya-Singhal@users.noreply.github.com>`.
  **No AI co-author / "Generated with" lines** in commits or files.
- **No paid APIs.** Free tiers only (Gemini API free tier, Gmail/Drive/Calendar APIs are fine).
- **Privacy rule:** never turn on the camera or microphone unless Aditya is present and says so. Anything that
  needs his voice (enrollment, voice tests, recordings) is his job; ask him and wait.
- **Keys and secrets:** the Gemini API key and the Google OAuth client secret are pasted by Aditya into the app's
  Settings, **never into the chat, a file, a commit or a log**. Never print them, never read them out of the DB.
- Ollama with `qwen2.5:7b` is installed on his Mac: **never delete it**.
- **Don't tag `v1.0.0`** without his explicit word.
- Tests must be green before every push:
  - Backend (`cd backend`): `.venv/bin/ruff check jarvis tests ../scripts`, `.venv/bin/mypy`,
    `.venv/bin/python -m pytest -m "not ollama" -q` (on the Mac also run plain `pytest -q`: the Mac-only tests run there).
  - UI (`cd app`): `npm run lint`, `npm test`, `npm run build`.
  - Shell (`cd app/src-tauri`): `cargo clippy --all-targets -- -D warnings`.
- Tests: `backend/tests/conftest.py` sets `JARVIS_FACE_AUTH=1`, `JARVIS_LLM_PROVIDER=ollama`,
  `JARVIS_EMBED_MODEL=bge-m3` so the older suites keep testing those paths. Voice-only / Gemini tests pass
  `face_auth=False` / `llm_provider="gemini"` explicitly (see `tests/test_voice_only.py`, `tests/test_gemini.py`,
  `tests/test_google*.py`).
- Before asking Aditya to test anything, self-test what you can without his voice/accounts (the suites,
  `scripts/eval_commands.py`, the headless backend `python -m jarvis.headless`). Only hand him checks that truly
  need him.
- If you run as a Remote Control session: an earlier attempt failed because every shell command was refused in
  auto mode. Ask Aditya to approve commands or switch the session out of auto mode.

---

## 2. What Aditya asked for (2026-10-01) and where it stands

| # | Requirement | Status |
|---|---|---|
| 1 | Extremely lightweight, runs all day | Built (cloud). **Measure on the Mac** (step 6) |
| 2 | Voice-only authentication, no face | Built. Face still available via `JARVIS_FACE_AUTH=1` |
| 3 | Stronger voice auth: long enrollment, every utterance checked incl. follow-ups, reject doubtful clips | Built. **Needs his enrollment + FAR/FRR measurement** (steps 4–5) |
| 4 | Clean Settings UI to connect Gmail, Drive, Calendar | Built (`GoogleCard.tsx`). **Needs his OAuth client + Connect** (step 3) |
| 5 | Not a hardcoded command list; the AI handles free-form requests | Built (tool catalogue in `llm/intents.py`). **Measure with real Gemini** (step 2) |
| 6 | Gemini brain, `gemma-4-26b-a4b-it` for commands, `gemini-3.5-flash-lite` for heavy tasks, quota fallback | Built. **No real Gemini call has ever been made** (cloud couldn't reach Google) |
| 7 | Light UI, professional look | Built (SVG orb, no rAF loops, new theme) |
| 8 | Instant "Yes boss, how may I help you?" | Built (pre-synthesised Kokoro clip, pinned in cache). **Check it's instant** |
| 9 | Conversation window 30 s / 1 min / 2 min with context | Built (`speech_service.py`, `brain.py`) |
| 10 | Read-back before sending mail, send only on verified "yes" or click | Built |
| 11 | Other laptop as a model server | **On hold. Do NOT build.** |

All of it is in PR https://github.com/Its-Aditya-Singhal/Jarvis/pull/3 (branch `claude/stoic-hypatia-ggx241`):
1153 backend tests + UI tests green, CI green. **Nothing from it has run on the Mac yet.**

What the PR contains, in short (details in `CHANGELOG.md` → Unreleased):
- `backend/jarvis/google/` (`auth.py` OAuth loopback + PKCE, refresh token sealed with the Keychain key;
  `gmail.py`, `drive.py`, `gcal.py` over httpx), `backend/jarvis/tools/google.py` (email.unread/summary/read/
  draft/send, drive.search/recent/open/summarize), Google Calendar merged into calendar.list/create.
- Per-utterance voice verdicts (verified / uncertain / rejected); uncertain only runs the `HARMLESS` set in
  `tools/runner.py`; short clips (< 0.8 s) are always uncertain (no longer stitched to the previous clip);
  24-phrase enrollment; `security.voice` defaults to Strict; `security.typed` defaults to off.
- Follow-up window (`voice.followup_s`, default 60 s), ends on "thanks / that's all", or when someone else talks.
- Lightweight: lazy `cv2`, `PerfMonitor` samples once a minute with no window open, no Ollama calls with Gemini,
  Waveform capped at 30 fps and paused when hidden.
- `scripts/eval_voice.py` (FAR/FRR from WAV folders).

---

## 3. Steps, in order

### Step 0. Merge and get the code onto the Mac
1. Aditya merges PR #3 into `main` on GitHub (or tells you to; you may not be allowed to push to `main` yourself
   from a cloud-style auto mode, so ask him if a push is refused).
2. In `~/Developer/jarvis`: `git status`. Old local edits there are superseded: **ask Aditya** before you
   `git stash` or discard anything, then `git checkout main && git pull --rebase`.
3. `cd backend && .venv/bin/pip install -e ".[dev]"` (recreate the venv with `python3.12 -m venv .venv` if it's
   missing), `.venv/bin/python ../scripts/download_models.py`, then `cd ../app && npm ci`.
4. Run every check from section 1, including the Mac-only tests (`pytest -q` without the marker filter). Fix
   anything red before going on (each fix = its own commit).

### Step 1. Build and launch
1. `scripts/build_dmg.sh` (see `docs/MAC_TEST_CHECKLIST.md` §2 for what to check: `codesign --verify`, missing
   PyInstaller modules show up in `~/Library/Application Support/JarvisAssistant/logs/backend.log` → add them to
   `PACKAGES` in `backend/sidecar/jarvis-backend.spec`). New modules from PR #3 that PyInstaller may miss:
   `jarvis.google.*`, `jarvis.tools.google`, `jarvis.llm.gemini`.
2. Install from the .dmg (or `npm run tauri dev` in `app/` for quick iterations) and open it.
3. **Check: the camera light never comes on**, from launch to quit (voice-only mode).

### Step 2. Gemini (Aditya pastes the key)
1. Aditya gets a free key at https://aistudio.google.com/apikey and pastes it into **Settings → AI → Gemini API
   key**, Save, **Test**. Test should say both models answered. If it fails, read the error (bad key, model name
   not found, quota) from the backend log; never ask him to paste the key elsewhere.
2. Measure with real requests (typed commands are off by default: either Aditya speaks, or temporarily allow
   typed commands in Settings → Security → Commands and turn it back off afterwards):
   - A command the patterns don't know, e.g. "could you get Notes up for me", "add milk to my Reminders",
     "turn on Do Not Disturb". Note the latency per model (the backend log has the timing).
   - **If `gemma-4-26b-a4b-it` is slow (> ~2 s) or returns broken JSON**, set the commands model to
     `gemini-3.5-flash-lite` as well (Settings → AI, or change the default in `brain.py` / `prefs.py` and its
     tests) and tell Aditya why.
   - Check the quota fallback message reads well (hard to force; at least read `Brain.chat` in `brain.py`).
3. Extend `scripts/eval_commands.py` so it can run the routing corpus against Gemini (today it only drives
   Ollama models: `OllamaClient` at line ~46). Add e.g. `--gemini` that builds a `Brain` with
   `llm_provider="gemini"` and takes the key from an env var **that Aditya sets in his own terminal**
   (`GEMINI_API_KEY`, never written to a file or echoed). Respect the free limits: add a delay between requests
   (Gemma ~30/min, Flash-Lite ~15/min) and an `--only` filter. Run it with `--model-only` for both models, report
   the pass rate, and fix routing failures in `llm/intents.py` (prompt / FEWSHOT) or `llm/fastpath.py`, with a
   corpus line in `backend/tests/corpus/routing.txt` for each fix.

### Step 3. Google account (Aditya does the console part)
1. Aditya follows **Settings → Google account → How to set up** (same steps as `docs/USER_GUIDE.md` → "Gmail,
   Google Drive and Google Calendar"): Cloud project, enable Gmail/Drive/Calendar APIs, OAuth consent screen
   External + himself as test user + **Publish app → In production**, OAuth client of type **Desktop app**.
2. He pastes the client ID + secret (or the JSON) into the card, ticks Gmail, Drive, Calendar, presses
   **Connect**, allows everything in the browser. Expect "CONNECTED · his address".
3. Test (by voice, his verified voice):
   - "summarize my last 10 emails" · "any new mail?" · "what did <a friend> mail me?"
   - → "okay, send him a mail confirming I'll come": it must save a Gmail draft, **read it back**, show the
     CONFIRM EMAIL card and wait. "yes, send it" sends it. **First send to his own address.** Check Gmail → Sent.
     Also check "no" leaves it as a draft, and that the card expires.
   - "draft a mail to <his own address> asking for the slides" → "send it"
   - "find <a file> in my drive" → "summarise it" → "open it"; "show my recent files in drive"
   - "what's on my calendar tomorrow?" includes Google Calendar events; "add dinner with Rahul on Friday at 8"
     appears in Google Calendar.
   - Disconnect → token revoked (Google Account → Security → Third-party access no longer lists it), reconnect works.
4. Anything wrong in the real API responses (fields, encodings, Hindi text, HTML-only mails, long threads) →
   fix in `backend/jarvis/google/*` or `tools/google.py` with a `httpx.MockTransport` test reproducing it
   (`tests/test_google.py`, `tests/test_google_tools.py`). **Never commit real mail content, addresses or tokens
   into tests**: use made-up data shaped like what you saw.

### Step 4. Voice enrollment and conversation (Aditya's voice, mic only with him present)
1. Settings → Identity → re-record voice (or the setup wizard): 24 phrases, quiet room, following each hint
   (normal / soft / loud / a step back / quick).
2. Check:
   - "Jarvis" alone → "Yes boss, how may I help you?" plays **at once** (no pause). If there is a delay, check
     the clip is pinned (`speech/output.py` `keep`, `speech_service.py` `ACK_PHRASE`) and not re-synthesised.
   - Follow-up window: after a reply, a follow-up without the name works for about a minute; "thanks" ends it
     ("Anytime."); someone else speaking closes it silently; the 30 s / 2 min settings work.
   - Context: "what did Rahul mail me" → "send him a mail…" knows who "him" is.
   - **Another person** (ask Aditya to get someone): "Jarvis, read my email" → "That voice doesn't match my
     owner. Command blocked." and also "Jarvis, volume up" is refused.
   - A short follow-up ("louder") right after the name works for Aditya.
   - How often Aditya himself gets "I couldn't confirm your voice". If it's often, note it for step 5.

### Step 5. Measure the voice match (FAR / FRR)
1. With Aditya's consent, he records: his enrollment-style phrases (~20 WAVs), ~20 short clips of himself on a
   **different day/room/volume**, and clips of other people **who agreed** (or public VoxCeleb clips).
   Keep them outside the repo (e.g. `~/voice/enroll`, `~/voice/owner`, `~/voice/others`), never commit them.
2. `backend/.venv/bin/python scripts/eval_voice.py --enroll ~/voice/enroll --owner ~/voice/owner --others ~/voice/others`
   and again with `--chunk 1.5` (short commands). Note FAR / FRR at Standard and Strict.
3. Goal: **FAR ≈ 0** at Strict (the requirement prefers rejecting a doubtful clip). If FAR > 0, raise the Strict
   thresholds in `prefs.VOICE_PRESETS`; if FRR is painful, tell Aditya the trade-off and let him choose.
4. Write the measured numbers into `docs/ML.md` (voice section) and `docs/SECURITY.md` (known limits), and delete
   the recordings when Aditya says so.

### Step 6. Measure lightness
1. Window closed, idle 10 minutes: Activity Monitor → the backend's CPU % (target ~0–1 %), memory (RSS),
   Energy Impact. Also with the window open and idle. Note JARVIS, `jarvis-backend` and (if running) Ollama.
2. Memory after an hour of normal use.
3. If idle CPU is high, profile (`py-spy top --pid <backend>` if installed, or `sample <pid>`) and fix the hot
   loop; the usual suspects are the mic/VAD loop, `perf.py`, and Whisper on non-addressed speech.
4. **Optional, only if Whisper is the cost:** Whisper still transcribes every speech segment the VAD lets
   through (and pads short clips) just to look for the wake word in text (`speech/wake.py`, `speech/stt.py`).
   The idea from the plan: a tiny wake-word gate before Whisper. Only build it if the measurement shows Whisper
   dominates idle-with-speech CPU, keep the wake word = the user's chosen name, and keep the current path as
   the fallback.
5. Write the numbers into `docs/ARCHITECTURE.md` / `README.md` where they claim "light".

### Step 7. The rest of the Mac checklist
Walk `docs/MAC_TEST_CHECKLIST.md`, especially the "Voice-only, Gemini and Google (October 2026)" section and
§6 "Every command (say each once)". Sections about face and liveness only apply with `JARVIS_FACE_AUTH=1`; skip
them unless Aditya wants face back. Also still open from the older plan (`docs/PLAN.md` section B):
`scripts/pin_models.py` to pin the model checksums, a fresh-user-account install test, Settings → Microphone
diagnostics. Fix what fails (test + commit each), tick the boxes in the checklist as you go.

### Step 8. Wrap up
- Update `CHANGELOG.md` (Unreleased → what was measured/fixed on the Mac), `docs/PLAN.md` section B ticks,
  and this file (or delete it when everything is done).
- Push to `main` after all checks are green.
- Ask Aditya before: tagging `v1.0.0`, publishing a release, enabling GitHub Pages.

---

## 4. Only Aditya can do
- Merge PR #3 (or allow the push to `main`).
- Paste the Gemini API key into Settings → AI (never in chat).
- Create the Google Cloud OAuth Desktop client, publish the consent screen, click Connect.
- Do the 24-phrase voice enrollment and the voice tests (and find another person for the rejection test).
- Record clips for `eval_voice.py` (his voice and consenting others).
- Approve shell commands if the session runs under Remote Control.
- Decide on the `v1.0.0` tag and the release.

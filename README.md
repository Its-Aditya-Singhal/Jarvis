# JARVIS — continuously authenticated personal assistant

A local-first desktop assistant for macOS (Apple Silicon) that keeps checking
*who* is in front of the computer before it will do anything. You pick its
name during setup (JARVIS, FRIDAY, anything). No paid APIs and no cloud: every
model runs on your Mac.

> **Status: Phase 9 of 10: identity, liveness, speech, local LLM, tools,
> auth levels, a trained fusion model, memory, and the privacy dashboard,
> settings and performance modes.** Packaging and the website come next.

## What works now

- Native macOS app (Tauri 2) with a command-center UI: animated core,
  status bar, navigation, live activity feed.
- First-run setup: your name, the assistant's name, and guided face enrollment
  (straight, left, right, up, down, closer, farther, smile, neutral). Each
  step checks that you actually did the pose before it records samples.
- Continuous face verification: approval needs several matching frames,
  expires unless it keeps being renewed, and locks when you leave.
- Stranger protection: an unknown face flips the app to **AUTHENTICATION
  DENIED**, logs a security event, and reveals no scores.
- Voice enrollment: six phrases in English, Hindi and Hinglish, using the
  names you chose. Recordings that are too short, too noisy, or sound like
  someone else are refused.
- Continuous voice verification: everything said near the Mac is checked
  against your voiceprint. Unknown voices are logged. The core, waveform and
  MIC indicator react to your voice live.
- Voice re-enrollment from the Authentication panel. Only the face-verified
  owner can do it, and it aborts if they leave.
- Liveness: a face match alone no longer unlocks anything. At unlock you
  get a short randomised challenge ("blink twice" plus a random turn left,
  turn right or move closer), and the check repeats at random times while
  the session is open. An anti-spoof model scores every frame, so photos and
  screens are blocked (**SPOOF DETECTED**). Frozen or looped camera feeds are
  rejected too.
- Speech: say the assistant's name to talk to it ("FRIDAY, what's the
  weather?", or just "FRIDAY?" and then the command). Speech recognition
  (Whisper) and the reply voice (Kokoro) both run on your Mac, in English
  and Hindi. You choose a female or male voice at setup, and can change it
  later in Settings.
  - Commands are accepted only from the verified owner (face + liveness). A
    different voice is refused even then.
  - Speech not addressed to the assistant by name is dropped straight after
    transcription. It is never shown, logged or stored.
- Local language model (Ollama, default `qwen2.5:7b`): answers questions in
  English or Hindi. It turns requests into structured intents, including
  compound ones ("wake me at 7 and note to buy milk" becomes two actions),
  and resolves times like "kal subah saat baje". Until the tools arrive, it
  carries out requests with real tools, and every spoken result is built
  from what actually happened, never from the model's wording.
  - Type commands too (English, हिंदी or Hinglish), owner only.
  - Switch models in Settings. If Ollama or the model is missing, the app
    says so plainly.
  - Voice enrollment now checks that you actually read the phrase shown.
- Tools (verified owner only, rechecked before each action):
  - **Alarms and timers:** chime, spoken reminder and macOS notification;
    dismiss, snooze, or just say "stop".
  - **Calendar and notes:** stored encrypted inside the app. Optional sync
    with Apple Calendar and Apple Notes, switched on in Settings.
  - **Opening apps:** any installed app by name, including in Hindi
    ("सफारी खोलो").
  - **File search:** Spotlight search in Documents, Desktop and Downloads,
    plus folders you add in Settings.
  - A Tools view lists alarms, upcoming events and notes.
  - Cancel alarms, and delete notes or calendar events (only after you
    confirm, see below).
- Auth levels, recomputed from live evidence for every action:
  - **L1 READ** (face + liveness): questions, reading the calendar, notes
    and files.
  - **L2 ACT** (plus your voice verified in the last minute, and nobody
    else in view): creating things, opening apps, cancelling alarms.
    Typed commands get L2 only within a minute of your voice being matched.
  - **L3 CONFIRM** (deletions): L2, a liveness check in the last 10
    minutes (one is triggered if needed), and an explicit "yes, go ahead"
    in your verified voice or a click on Confirm within 30 s.
  - A trained fusion classifier combines face, liveness and voice scores.
    It can only lower a level, never raise one.
  - Stranger protection: an unknown voice speaking drops you to L1 until
    you speak again. Someone else in view caps you at L1. A photo or an
    unknown face means L0.
- Memory:
  - Say "remember that my sister's birthday is 12 March" (or "yaad rakhna
    ki…"), then later ask "when is my sister's birthday?" in English, Hindi
    or Hinglish.
  - When you mention something personal ("my exam is on Friday"), the
    assistant offers **Save to memory?** as a chip. Nothing is saved without
    your tap.
  - "Forget …" goes through the level-3 confirmation.
  - The Memory view lists, edits and deletes facts, and searches or clears
    conversation history (kept 30 days by default; off, 7 days or forever
    in the same view).
  - If a personal detail isn't in memory, the assistant says it doesn't
    know rather than guessing.
- Speed: easy commands (timers, alarms, opening apps, notes, time and date,
  calendar, file search, remember and forget, including Hinglish) skip the
  language model entirely. Speech recognition runs on the Apple GPU, and
  saying just the name answers with an instant ping.
- Security log visible only while the verified owner is at the screen.
- Privacy dashboard (Privacy view):
  - Lists every kind of stored data with its size, age and protection:
    face and voice profiles, memory, history, notes/alarms/events, fusion
    samples, the security log and settings. It also lists what is never
    stored.
  - Delete any of them, export a readable JSON copy (templates are never
    exported), or factory reset. Every one needs level 3.
  - Deleting the face profile goes straight to a new face scan. If that
    window is missed, saying the assistant's name in your verified voice
    unlocks the scan again.
- Offline mode, on by default: the backend refuses every connection except
  to itself and Ollama on 127.0.0.1. The Privacy view lists blocked attempts
  and any open internet connections, including the Ollama server's. The
  status bar shows **OFFLINE ✓**.
- Settings in one place:
  - Identity: rename yourself or the assistant, which also changes the
    wake word. Re-scan your face (your old profile stays until the new one
    is saved) or re-enroll your voice.
  - Voice: speed, beep or "Yes?", and the follow-up window.
  - Security presets.
  - Performance modes, models, memory switches, file-search folders, Apple
    sync and the fusion model.
- Performance modes, **Fast / Balanced / Quality**. Auto (the default) uses
  Balanced when plugged in and Fast on battery. The status bar shows the
  mode, and Settings shows CPU, memory and loaded-model use.
- Problems are explained with a fix: camera or microphone blocked, a model
  missing, Ollama not running, low disk space or memory. They appear on the
  System view, and serious ones are spoken once after startup.

## Architecture

```
app/                    Tauri 2 shell + React/TypeScript UI
  src-tauri/src/lib.rs  starts the backend on a random loopback port with a per-launch token
  src/components/       Orb (canvas), StatusBar, SideNav, ActivityFeed, CameraPreview
  src/pages/            Setup wizard, Main command center
backend/jarvis/
  api/app.py            FastAPI REST + WebSocket (127.0.0.1 only, token-guarded)
  camera/capture.py     camera capture in the backend: the UI can't inject frames
  audio/mic.py          microphone capture in the backend (16 kHz), same reasoning
  auth/face/            engine, quality, enrollment (training), continuous (inference)
  auth/voice/           Silero VAD + segmenter, ECAPA engine, quality, enrollment, verification
  auth/liveness/        passive anti-spoof, blink detector, challenges, replay checks, gate
  speech/               Whisper STT, Kokoro TTS, playback queue, wake word, script-independent text matching
  auth/matching.py      top-k cosine template matching shared by face and voice
  security/             Keychain key, AES-256-GCM template store
  database/db.py        SQLite: profile + security events
  service.py            camera → face engine → enrollment / continuous verification + liveness gate
  voice_service.py      mic → VAD → speaker embedding → enrollment / verification → speech
  speech_service.py     transcription, wake word, owner checks, spoken replies
  llm/                  Ollama server manager + client, intent schema and prompt
  brain.py              command → intent (JSON) → reply; short in-memory context
  tools/                encrypted store, runner, alarm scheduler, app index, file search, Apple bridge
models/                 downloaded models (git-ignored)
scripts/download_models.py
```

## How the face ML works

| Stage | Model / method |
|---|---|
| Detection | SCRFD-10G (InsightFace `buffalo_l`), ONNX Runtime with CoreML |
| Alignment | 5-point similarity transform to 112×112 |
| Embedding | ArcFace ResNet-50 trained on WebFace600K, 512-d, L2-normalised |
| Head pose / expression | 68-point 3D landmark model (pitch/yaw/roll, mouth-to-eye ratio) |
| Eyelids | 106-point 2D landmark model (eye contours for blink detection) |
| Quality gate | detector score × face size × sharpness (Laplacian variance) × exposure |
| Matching | mean of top-5 cosine similarities to the enrolled set |
| Decision | median over a 6-frame window: ≥ 0.42 approve, < 0.25 stranger, hysteresis while approved |

## How liveness works

The face matcher decides *who* is present. The liveness gate decides whether
that face belongs to a live person. The owner counts as verified only when
both agree.

| Layer | Method |
|---|---|
| Passive anti-spoof | InsightFace liveness addon: 80×80 CNN on a 5-point aligned crop → live probability, every frame |
| Blink | eye openness = contour height/width (106-pt) × pixel contrast inside the eye vs. face brightness; a blink is a drop below 0.75× the person's own rolling baseline that reopens within 0.9 s |
| Challenge-response | "blink twice" plus random others (turn left ≥ 18°, turn right, move 25% closer), random order, 8 s per step; analysis runs at 12 fps during a challenge |
| Continuity | the face may not jump position or size between frames mid-challenge (phone swapped in) |
| Replay heuristics | bit-identical frames for 2 s = virtual/frozen feed; no natural blink for 150 s → re-challenge |
| Decisions | passing a challenge also needs a median live score ≥ 0.6 during it; a sustained median < 0.25 = spoof; 0.25–0.6 while unlocked → immediate re-challenge |
| Session | re-challenge at a random time 5–15 min after each pass; liveness is lost after 5 s out of view; 3 failures → 60 s lockout |

Calibration so far: direct photos scored 0.92–0.99, while downscaled or
recaptured faces scored 0.001–0.06. Closed eyes measured 37% of open-eye
openness on a webcam-sized face. These results come from sample images, not
a live webcam, so check the anti-spoof score (Authentication panel, owner
only) on your own camera. If a real face sits below 0.6, lower
`JARVIS_LIVENESS_PASS_THRESHOLD`.

## How speech works

| Stage | Model / method |
|---|---|
| Speech to text | Whisper `small` on the Apple GPU (MLX), about 0.3 s per command on an M1 Pro. A decoding loop is detected and retried without the name prompt. Falls back to faster-whisper on the CPU (about 1.0–1.3 s) |
| Language | Whisper's detection, limited to English or Hindi (decoded again if it picks another language) |
| Wake word | the chosen name, matched in the transcript among the first words, tolerant of spelling and script ("Friday" / "फ्राइडे"); the name is given to Whisper as a descriptive prompt |
| Hindi / Hinglish matching | Devanagari → Latin transliteration + consonant skeletons, so "subah" = "सुबह" = "subaha" |
| Text to speech | Kokoro-82M (ONNX fp32, 8 threads, about 0.25× real time; fp32 is about 3× faster than int8 on Apple Silicon): female `af_heart` / male `am_michael`; Hindi text switches to `hf_alpha` / `hm_omega` |
| Playback | the next sentence is synthesised while the current one plays; a long first sentence starts at its first comma; short phrases are cached and common ones pre-built at startup; the mic is muted while speaking plus 0.3 s |
| End of speech | 0.45 s of silence ends an utterance; a bare name answers with a ping instead of a spoken "Yes?" |

Measured on an M1 Pro, from the moment you stop speaking:

| Request | Before phase 8 | Now |
|---|---|---|
| Name alone, until it's listening | about 2.5 s | about 0.8 s (ping) |
| Easy command ("set a timer for 5 minutes"), until the reply starts | about 4 s | about 1.3–1.8 s |
| Question for the model ("who wrote Hamlet?"), until the reply starts | about 5 s | about 2.5 s |

## How the language model is used

| Part | Detail |
|---|---|
| Runtime | Ollama on 127.0.0.1:11434, started by JARVIS if not already running; models kept in `~/Developer/ollama/models` |
| Default model | `qwen2.5:7b` (Q4, about 4.7 GB); any installed model can be picked in Settings |
| Output | JSON schema enforced by Ollama: `language` (en/hi/hinglish), `actions` (tool + args + summary), `reply` |
| Tool catalogue | alarm.set/cancel, timer.set, calendar.create/list/delete, notes.add/search/delete, app.open, files.search, memory.remember/forget, history.search. Unknown tools are dropped |
| Safety | the model never executes anything; replies to action requests are composed by code; no shell, web or messaging |
| Context | last 4 exchanges, in memory only, expire after 5 min and are cleared when the owner leaves |
| Fast path | `llm/fastpath.py` understands common commands with patterns (English, Hinglish, and Devanagari via transliteration) in under 1 ms. Anything it doesn't fully match goes to the model |
| Prompt cache | the system prompt and examples are identical for every request and primed at startup, so Ollama only evaluates the new message (0.16 s instead of 6 s). Background memory requests reuse the same prefix, and Ollama runs 2 slots so they never block a command |

## How tools work

| Tool | How | Level* |
|---|---|---|
| alarm.set / timer.set / alarm.cancel | encrypted SQLite + in-app scheduler (rings only while JARVIS runs; alarms > 10 min overdue at startup are marked missed) | 2 |
| calendar.create / list | encrypted local events; with Apple sync on, also created in the chosen Apple calendar and listed from all Apple calendars | 2 / 1 |
| notes.add / search | encrypted local notes (fuzzy search); with sync on, also in Apple Notes folder "JARVIS" | 2 / 1 |
| app.open | any `.app` in the standard Applications folders, fuzzy and Devanagari-aware name match, opened with `open <bundle>` (no shell, no arguments) | 2 |
| files.search | `mdfind -onlyin` per allowed folder; file names only; system, library and hidden folders refused | 1 |
| notes.delete / calendar.delete | resolved to one exact item first (fuzzy match), shown in the confirmation, deleted only after it; copies in Apple's apps are left alone | 3 |

\*See "How auth levels and fusion work". Level-3 tools cannot be run
directly: the runner only plans them, and deletion happens after confirmation.
Model output is untrusted: each tool validates its arguments (times in the
future, sane timer lengths, known apps, allowed folders). AppleScript
receives values as `argv`, never as script text.

## Privacy, settings and performance

| Part | Detail |
|---|---|
| Levels | ordinary preferences need L1. Security presets and names need L2. Loosening a security preset, allowing the network, and every deletion, export or factory reset need an L3 confirmation (spoken or clicked) |
| Security presets | face match Standard/Strict (cosine 0.42/0.50), voice Standard/Strict (0.50/0.58), lock after 8 s / 20 s / 1 min away, random liveness checks every 5–15 or 2–5 min. There are no free-form numbers, so nothing can be set to an unsafe value |
| Factory reset | stops verification, deletes templates and the personal fusion model, empties every table (then `VACUUM`), and deletes the Keychain key, so anything left on disk can't be decrypted |
| Locked out | if no face profile, no usable voice profile and no re-scan window remain, nobody can ever be verified. Only then is a reset allowed without verification: it erases and never reveals |
| Export | JSON written with `0600` permissions to a path you pick in the save dialog, never inside the app's data folder |
| Offline guard | wraps socket connect and DNS lookups for the whole backend process; loopback always passes. `HF_HUB_OFFLINE` stops libraries from trying to download. Ollama is a separate process, so its connections are observed and shown, not blocked |
| Modes | Fast: the fast-mode model (default `qwen2.5:3b`), Whisper small, 4 face checks/s, no memory suggestions. Balanced: your model, Whisper small, 6/s. Quality: your model, Whisper medium, 8/s. A model that isn't downloaded is reported, not faked |

## How memory works

| Part | Detail |
|---|---|
| Facts | saved when you say "remember…" (level 2), type one in the Memory view, or tap a suggestion; near-duplicates update the existing fact |
| Recall | each question to the model is embedded with **bge-m3** (multilingual, via Ollama, about 0.1 s). The closest facts (cosine ≥ 0.45) travel with the message as `[Remembered: …]`. Without the embedding model, fuzzy word matching is used |
| Suggestions | only for chat turns that sound personal ("my", "I'm", "mera", "mujhe"…), run after the reply is spoken. Relative dates are spelled out in code first ("Friday" becomes "Friday 2 October 2026") because the model gets date arithmetic wrong |
| History | only turns addressed to the assistant, as text; kept for 30 days (off, 7 days or forever); semantic search ("test" finds "exam") |
| Storage | facts, history and their embeddings are sealed with AES-256-GCM (embeddings too: a sentence vector can leak its meaning). Audio is never stored |
| Levels | recall and history search L1, remember L2, forget L3 (confirmation); editing or deleting in the Memory view needs L2 |

## How the voice ML works

| Stage | Model / method |
|---|---|
| Voice activity | Silero VAD v5 (ONNX), 32 ms frames, hysteresis 0.5 / 0.35 |
| Segmentation | utterance ends after 0.55 s of silence; 0.2 s pre-roll; 12 s cap |
| Features | 80-bin log-Mel filterbank, per-utterance mean normalisation |
| Embedding | ECAPA-TDNN (SpeechBrain, trained on VoxCeleb 1+2), 192-d, L2-normalised |
| Quality gate | speech duration × SNR vs. measured noise floor × level × clipping |
| Enrollment | 6 phrases → whole-clip + 2 s sliding-window embeddings (~20 vectors) |
| Decision | top-5 cosine: ≥ 0.50 verified, < 0.30 unknown voice, noisy audio never rejects |

Calibration with synthetic macOS voices standing in for different speakers:
the true speaker scored 0.64–0.77 on unseen sentences and other voices 0.27 or
below. The exception was two Apple Indian-English voices that are near-clones
of each other (about 0.52). That is why the fusion model below combines the
factors instead of relying on either threshold alone.

**Enrollment (training)** collects about 36 face embeddings across poses. Frames
are discarded immediately and only the embeddings are kept, encrypted.
**Verification (inference)** compares each live embedding with that template.

## How auth levels and fusion work

Hard rules come first, then the fusion classifier (see `backend/jarvis/auth/levels.py`):

| Level | Rules | Fusion score |
|---|---|---|
| L1 READ | face approved + liveness passed | presence ≥ 0.5 |
| L2 ACT | L1 + owner voice verified ≤ 60 s ago, no unknown voice since, no bystander | command ≥ 0.8 |
| L3 CONFIRM | L2 + liveness ≤ 10 min old + explicit confirmation (verified voice or click, 30 s) | command ≥ 0.9 |

- **Features:** 9 scores only, never embeddings: face similarity, face
  freshness, face quality, passive anti-spoof, liveness freshness, and voice
  heard / match / freshness, plus other faces in view.
- **Model:** L2-regularised logistic regression on the features and all
  their pairwise products, fitted with Newton's method in NumPy. Inference is
  one dot product. The *presence* score is the same model with the voice
  features removed, so people talking nearby can't lock you out of reading.
- **Training data:** there is no public dataset of paired face, voice and
  liveness scores. The shipped model is trained on 20,000 simulated sessions
  whose score distributions follow what the pipelines produce here:
  - owner speaking, owner silent, and owner in hard conditions;
  - strangers, look-alikes, photos, replayed video, someone else speaking,
    and stale evidence.
- **Personal retraining:** while you use the app it keeps feature vectors
  (scores only) from your own commands, and from strangers, spoofs and
  unknown voices it catches. Settings → Fusion → *Retrain with my data*
  weights these 3× against the simulated ones. This needs L2.
- **Comparison:** `scripts/train_fusion.py` regenerates the shipped model and
  compares it with scikit-learn models on held-out simulated data:

| Model | Accuracy | ROC AUC | FAR / FRR at L2 (0.8) |
|---|---|---|---|
| Linear logistic regression | 93.4% | 0.975 | 5.8% / 10.3% |
| **Poly-2 logistic regression (shipped)** | 97.8% | 0.997 | 1.6% / 4.2% |
| Random forest (200 trees) | 98.8% | 0.998 | 1.4% / 1.9% |
| Gradient boosting | 98.8% | 0.998 | 1.7% / 1.4% |

The poly-2 model is within a point of the tree ensembles, runs in NumPy with
no extra dependency, and is easy to inspect. With the hard rules added, no
simulated stranger, look-alike, photo or other-voice session reaches L2. The
simulated replayed video that also passed a liveness challenge reaches it in
about 4% of cases. These are simulated numbers and should not be read as
field accuracy.

## Security model

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
  way. Fusion samples hold scores only. Nothing is sent anywhere: recall and
  suggestions use the local Ollama models.
- Security events: unknown face or voice, spoof suspected, liveness check
  failed, liveness lockout, frozen camera feed, voice command while not
  verified, command in a non-owner voice, and a tool blocked because the
  level was too low (or the owner left mid-command). Confirmed deletions,
  privacy actions, security-setting changes, face re-scans, factory resets
  and fusion retraining are also logged. Each event records the time, the
  outcome, and whether access was blocked.
- **Known gaps:** voice alone has no replay protection, so a recording of
  the owner's voice may pass voice verification. Acting on it still requires
  the live owner in front of the camera (L2 needs face, liveness and voice
  together). You
  can't interrupt the assistant while it speaks (it doesn't listen then). The
  passive anti-spoof model is a small CNN: a high-quality 3D mask or a
  real-time deepfake piped into a virtual camera is beyond what it and the
  challenges can guarantee.

## Setup (developer)

Requirements: macOS on Apple Silicon, Python 3.12, Node 20+, Rust (`brew install rustup && rustup default stable`).

```bash
cd backend && python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python ../scripts/download_models.py   # ~1.8 GB, one time (add "medium" for Quality mode)
.venv/bin/python -m pytest                        # 209 tests (LLM tests need the model)
brew install ollama && mkdir -p ~/Developer/ollama/models
OLLAMA_MODELS=~/Developer/ollama/models ollama serve &   # JARVIS also starts it itself
ollama pull qwen2.5:7b                            # ~4.7 GB
ollama pull bge-m3                                # ~1.2 GB, memory recall
ollama pull qwen2.5:3b                            # ~1.9 GB, Fast mode (optional)
cd ../app && npm install && npm run tauri dev
```

Optional: regenerate the fusion model and compare it with scikit-learn models:
`cd backend && .venv/bin/pip install -e ".[train]" && .venv/bin/python ../scripts/train_fusion.py`.

On first launch macOS asks for camera and microphone access for the app (in
dev: for your terminal). The voice tests use the built-in `say` voices, so
they run only on macOS.

InsightFace model weights are licensed for non-commercial research use,
which is fine for this academic project.

## Roadmap

1. ✅ Foundation + face identity
2. ✅ Voice enrollment + speaker verification (ECAPA)
3. ✅ Liveness (passive anti-spoof + blink/turn challenges)
4. ✅ Speech I/O (faster-whisper STT, Kokoro TTS, wake word = assistant name)
5. ✅ Local LLM via Ollama (configurable model, structured intents)
6. ✅ Tools: alarm, calendar, notes, app launcher, file search (Apple sync optional)
7. ✅ Continuous multi-factor auth + trained fusion model, auth levels 1–3
8. ✅ Memory (encrypted facts + history, bge-m3 recall, suggestions) and a speed pass
9. ✅ Privacy dashboard, settings, performance modes, offline guard, health checks
10. Polish, `.dmg` packaging, download website, full docs

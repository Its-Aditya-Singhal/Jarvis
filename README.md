# JARVIS — continuously authenticated personal assistant

A local-first desktop assistant for macOS (Apple Silicon) that keeps checking
*who* is in front of the computer before it will do anything. You pick its
name during setup (JARVIS, FRIDAY, anything). No paid APIs and no cloud: every
model runs on your Mac.

> **Status: Phase 3 of 10: face, voice and liveness.** Speech recognition,
> the local LLM and tools arrive in later phases. The UI marks
> every unbuilt feature as such rather than faking it.

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
- Security log visible only while the verified owner is at the screen.

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
  auth/matching.py      top-k cosine template matching shared by face and voice
  security/             Keychain key, AES-256-GCM template store
  database/db.py        SQLite: profile + security events
  service.py            camera → face engine → enrollment / continuous verification + liveness gate
  voice_service.py      mic → VAD → speaker embedding → enrollment / verification
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
of each other (about 0.52). Phase 7 combines face, voice and liveness in a
trained fusion model instead of relying on either threshold alone.

**Enrollment (training)** collects about 36 face embeddings across poses. Frames
are discarded immediately and only the embeddings are kept, encrypted.
**Verification (inference)** compares each live embedding with that template.
A trained fusion classifier over face, voice and liveness comes in phase 7.

## Security model

- Biometric templates are embeddings only (no photos), sealed with AES-256-GCM.
  The key lives in the macOS Keychain, so a copied template file can't be read.
  Tampering is detected on load.
- Biometrics are identifiers, not passwords. They can't be changed if leaked,
  which is why they never leave the device.
- The backend binds to 127.0.0.1 and requires a random token for each launch.
- Once setup is complete, the profile and face enrollment endpoints are
  locked. Re-enrollment will need a verified owner (privacy dashboard,
  phase 9).
- Audio is processed in memory and discarded. Only voice embeddings are kept,
  sealed the same way as face templates.
- Security events: unknown face or voice, spoof suspected, liveness check
  failed, liveness lockout, frozen camera feed. Each records time, outcome
  and whether access was blocked.
- **Known gaps:** voice has no replay protection yet, so a recording of the
  owner may pass voice verification. Phase 4 adds spoken-phrase checks, and
  phase 7 fuses voice with face and liveness. Until phase 4, enrollment
  doesn't check that the displayed phrase was the one actually spoken. The
  passive anti-spoof model is a small CNN: a high-quality 3D mask or a
  real-time deepfake piped into a virtual camera is beyond what it and the
  challenges can guarantee.

## Setup (developer)

Requirements: macOS on Apple Silicon, Python 3.12, Node 20+, Rust (`brew install rustup && rustup default stable`).

```bash
cd backend && python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python ../scripts/download_models.py   # ~370 MB, one time
.venv/bin/python -m pytest                        # 56 tests
cd ../app && npm install && npm run tauri dev
```

On first launch macOS asks for camera and microphone access for the app (in
dev: for your terminal). The voice tests use the built-in `say` voices, so
they run only on macOS.

InsightFace model weights are licensed for non-commercial research use,
which is fine for this academic project.

## Roadmap

1. ✅ Foundation + face identity
2. ✅ Voice enrollment + speaker verification (ECAPA)
3. ✅ Liveness (passive anti-spoof + blink/turn challenges)
4. Speech I/O (faster-whisper STT, Piper/Kokoro TTS, wake word = assistant name)
5. Local LLM via Ollama (configurable model)
6. Tools: alarm, calendar, notes, app launcher, file search (permissioned)
7. Continuous multi-factor auth + trained fusion model, auth levels 1–3
8. Memory (SQLite + local embeddings)
9. Privacy dashboard + settings + performance modes
10. Polish, `.dmg` packaging, download website, full docs

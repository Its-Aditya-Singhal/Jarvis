# How the models work

Everything below runs on the Mac. Measured numbers come from an M1 Pro unless a
line says otherwise; simulated numbers are marked as simulated.

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
| Enrollment | 24 phrases (English, Hindi, Hinglish; normal, soft, louder, from a step back) → whole-clip + 2 s sliding-window embeddings (~70 vectors) |
| Decision | top-5 cosine, Strict preset by default: ≥ 0.58 verified, < 0.35 unknown voice (Standard: 0.50 / 0.30); noisy audio and clips under 1.5 s never reject; a clip under 0.8 s of speech is always unclear (never joined to the clip before it) |
| When | every utterance addressed to the assistant (the name, or the conversation window), judged on its own; `scripts/eval_voice.py` measures false accepts / rejects |

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

## How memory works

| Part | Detail |
|---|---|
| Facts | saved when you say "remember…" (level 2), type one in the Memory view, or tap a suggestion; near-duplicates update the existing fact |
| Recall | each question to the model is embedded with **bge-m3** (multilingual, via Ollama, about 0.1 s). The closest facts (cosine ≥ 0.45) travel with the message as `[Remembered: …]`. Without the embedding model, fuzzy word matching is used |
| Suggestions | only for chat turns that sound personal ("my", "I'm", "mera", "mujhe"…), run after the reply is spoken. Relative dates are spelled out in code first ("Friday" becomes "Friday 2 October 2026") because the model gets date arithmetic wrong |
| History | only turns addressed to the assistant, as text; kept for 30 days (off, 7 days or forever); semantic search ("test" finds "exam") |
| Storage | facts, history and their embeddings are sealed with AES-256-GCM (embeddings too: a sentence vector can leak its meaning). Audio is never stored |
| Levels | recall and history search L1, remember L2, forget L3 (confirmation); editing or deleting in the Memory view needs L2 |


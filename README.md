# JARVIS

**A private voice assistant for the Mac that keeps checking it's you.**

JARVIS listens for its name (you choose it: JARVIS, FRIDAY, anything) and does
everyday things by voice, in English, Hindi or Hinglish. Before it acts, it
checks your face, that you're a live person and not a photo, and your voice.
Every model runs on your Mac. No paid APIs, no cloud, no account.

[Website](https://its-aditya-singhal.github.io/Jarvis/) ·
[Download](https://github.com/Its-Aditya-Singhal/Jarvis/releases/latest) ·
[User guide](docs/USER_GUIDE.md) · [Security](docs/SECURITY.md) ·
[How the models work](docs/ML.md) · [Architecture](docs/ARCHITECTURE.md) ·
[Changelog](CHANGELOG.md)

## What it does

- **Knows who's there.** ArcFace face verification over several frames, a
  passive anti-spoof model plus random liveness challenges, and an ECAPA
  voiceprint. A fusion classifier combines them into levels: reading needs
  your live face, acting also needs your voice, deleting also needs your "yes".
  Strangers, photos, frozen feeds and other voices are refused and logged.
- **Does everyday things.** Alarms and timers, calendar and notes (optional
  Apple sync), opening and quitting apps, folders and websites, volume and
  music, brightness, dark mode, screenshots, clipboard, typing, finding recent
  files, showing them in Finder or moving them to the Trash, math and unit
  conversions, and "ask Claude/ChatGPT to…" with the prompt written in for you.
- **Remembers, privately.** "Remember that…" facts and conversation history,
  recalled by meaning with a local embedding model, all encrypted.
- **Stays on your Mac.** Whisper for speech recognition on the Apple GPU,
  Kokoro for the voice, a local language model through Ollama. The engine is
  offline by default and shows anything that tries to connect.
- **Shows its work.** A privacy dashboard with every kind of stored data, its
  size and age, and export or delete for each; a security log; plain-language
  fixes for every problem it detects.

Measured on an M1 Pro, from the end of speech: about 0.8 s until it's listening
after its name, 1.3–1.8 s until the reply starts for an easy command, about
2.5 s for a question to the language model.

## Install

Requires macOS 14 or later on Apple Silicon, 16 GB of memory recommended and
about 10 GB of free space. Download the .dmg from the
[latest release](https://github.com/Its-Aditya-Singhal/Jarvis/releases/latest),
drag JARVIS to Applications, then open it and allow it once in System Settings
→ Privacy & Security (it is signed but not notarized). On first launch it
downloads its models and helps you install Ollama. The
[user guide](docs/USER_GUIDE.md) walks through it.

## Developer setup

Requirements: macOS on Apple Silicon, Python 3.12, Node 20+, Rust (`brew install rustup && rustup default stable`).

```bash
cd backend && python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python ../scripts/download_models.py   # ~1.7 GB, checksummed (add "medium" for Quality mode)
.venv/bin/python -m pytest                        # LLM tests need Ollama and the model
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

Build the .dmg on an Apple Silicon Mac with `scripts/build_dmg.sh` (see
[ARCHITECTURE.md](docs/ARCHITECTURE.md#first-run-and-packaging)); pushing a
`v*` tag does the same on GitHub Actions and drafts a release.

## Licence

JARVIS's own code is under the [MIT licence](LICENSE).

The models JARVIS downloads have their own licences: InsightFace's face models are for non-commercial research
use; Whisper, Kokoro, ECAPA (SpeechBrain), Silero VAD and Qwen 2.5 are under
permissive licences. Check them before using JARVIS commercially.

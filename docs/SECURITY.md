# Security

JARVIS is a voice assistant that can change things on your Mac, so it keeps
checking who is in front of it. This page says what it protects against, how,
and where that stops.

## Threat model

| Who | Example | What stops them |
|---|---|---|
| Someone else at your unlocked Mac | a flatmate says "FRIDAY, delete my notes" | an unknown face means level 0; another face in view caps you at level 1; a non-owner voice is refused and logged |
| A photo, screen or looped video of you | your picture held up to the camera | the passive anti-spoof CNN, random liveness challenges (blink, turn, lean in), continuity and frozen-feed checks |
| A recording of your voice | a clip played from a phone | acting needs level 2: face, liveness **and** voice together, so a recording alone does nothing |
| Other programs on the Mac | a web page or local app calling the backend | the API binds to 127.0.0.1, needs a fresh random token each launch, rejects foreign Host and Origin headers (DNS rebinding) |
| The language model itself | a prompt that makes it "decide" to delete files | the model only proposes structured intents; code validates each one, re-checks the live auth level before acting and composes the reply; there is no shell, web or messaging tool |
| Someone who copies your disk | the app's data folder on a stolen backup | templates, memory, history, notes and events are sealed with AES-256-GCM; the key is in the macOS Keychain |
| The network | a library phoning home | the offline guard refuses every non-loopback connection from the backend process and lists attempts |

**Out of scope:** an attacker with your macOS login or admin rights (they can
read the Keychain and the app's memory), a malicious macOS or hardware, and
physical coercion.

## Levels

Every action is authorised from live evidence at the moment it runs (see
[ML.md](ML.md#how-auth-levels-and-fusion-work) for the fusion model):

| Level | Needs | Allows |
|---|---|---|
| L0 | nothing verified | nothing; the security log is hidden |
| L1 READ | your face over several frames + passed liveness | questions, reading calendar, notes, files, memory |
| L2 ACT | L1 + your voice verified in the last 60 s, no unknown voice since, nobody else in view | alarms, notes, opening apps, controlling the Mac, typing, Ask Claude/ChatGPT |
| L3 CONFIRM | L2 + liveness in the last 10 min + an explicit yes (verified voice or a click) within 30 s | deleting notes, events, memories, moving files to the Trash, privacy actions |

Level-3 tools are never run directly: the runner plans them, shows exactly what
will be affected, and executes only after the confirmation. A confirmation is
single-use and bound to that plan.

## Protections in detail

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

## Privacy controls

| Part | Detail |
|---|---|
| Levels | ordinary preferences need L1. Security presets and names need L2. Loosening a security preset, allowing the network, and every deletion, export or factory reset need an L3 confirmation (spoken or clicked) |
| Security presets | face match Standard/Strict (cosine 0.42/0.50), voice Standard/Strict (0.50/0.58), lock after 8 s / 20 s / 1 min away, random liveness checks every 5–15 or 2–5 min. There are no free-form numbers, so nothing can be set to an unsafe value |
| Factory reset | stops verification, deletes templates and the personal fusion model, empties every table (then `VACUUM`), and deletes the Keychain key, so anything left on disk can't be decrypted |
| Locked out | if no face profile, no usable voice profile and no re-scan window remain, nobody can ever be verified. Only then is a reset allowed without verification: it erases and never reveals |
| Export | JSON written with `0600` permissions to a path you pick in the save dialog, never inside the app's data folder |
| Offline guard | wraps socket connect and DNS lookups for the whole backend process; loopback always passes. `HF_HUB_OFFLINE` stops libraries from trying to download. Ollama is a separate process, so its connections are observed and shown, not blocked |
| Modes | Fast: the fast-mode model (default `qwen2.5:3b`), Whisper small, 4 face checks/s, no memory suggestions. Balanced: your model, Whisper small, 6/s. Quality: your model, Whisper medium, 8/s. A model that isn't downloaded is reported, not faked |

## First-run model download

Models are not shipped inside the app. The first-run download is the only time
the backend reaches the internet, and only because you pressed Download:

- The list of files, their URLs and SHA-256 pins is fixed inside the app
  (`backend/jarvis/model_manifest.json`); the endpoint accepts pack names, never URLs.
- The offline guard opens only GitHub and Hugging Face, only for the download
  thread, only while it runs. Redirects to any other host are refused.
- Every file is hashed and compared with its pin before it is moved into place;
  a mismatch deletes it. Release builds refuse to build with an unpinned file
  (`scripts/pin_models.py --check`).
- Archives are unpacked by listed file name only, so an archive can't write
  outside the models folder.
- Ollama downloads the language models itself, as a separate process; its
  connections are shown in the Privacy view.

## Known limits

- **Voice replay:** voice verification alone has no replay protection. That is
  why acting also needs your live face.
- **Masks and deepfakes:** the passive anti-spoof model is a small CNN. A
  high-quality 3D mask or a real-time deepfake fed into a virtual camera is
  beyond what it and the challenges can guarantee.
- **Look-alikes and twins:** face thresholds are tuned for general use; a close
  relative may score near the threshold. The Strict preset raises it.
- **No interruption while speaking:** the microphone is muted while the
  assistant talks.
- **Signing:** releases are signed ad hoc, not notarized by Apple, so macOS
  can't vouch for who built the app. Check the published SHA-256 of the .dmg,
  or build it yourself with `scripts/build_dmg.sh`.
- **Fusion numbers are simulated:** the classifier's accuracy figures come from
  simulated sessions, not field trials.

## Reporting a problem

Open an issue on GitHub, or for anything sensitive contact the maintainer
privately through their GitHub profile before publishing details.

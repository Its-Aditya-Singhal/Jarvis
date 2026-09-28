# User guide

JARVIS is a voice assistant for your Mac that only works for you. It watches
for your face, checks that you're a live person, and listens for your voice
before it does anything. Every model runs on your Mac.

## What you need

- A Mac with Apple Silicon (M1 or newer) and macOS 14 Sonoma or later
- 16 GB of memory recommended
- About 10 GB of free disk space (models: about 1.7 GB, plus about 6 GB for the
  language models in Ollama)
- A camera and a microphone (the built-in ones are fine)
- An internet connection for the first-run download only

## Install

1. Download `JARVIS-1.0.0-arm64.dmg` from the
   [latest release](https://github.com/Its-Aditya-Singhal/Jarvis/releases/latest).
   Optionally compare its SHA-256 with `SHA256SUMS.txt` on the same page:
   `shasum -a 256 ~/Downloads/JARVIS-1.0.0-arm64.dmg`.
2. Open the .dmg and drag **JARVIS** onto **Applications**.
3. Open JARVIS from Applications. The app is signed but not notarized by Apple,
   so macOS stops it the first time. Go to **System Settings → Privacy &
   Security**, scroll down, and press **Open Anyway** next to JARVIS. You only
   do this once.
4. macOS asks for the **camera** and the **microphone**. Allow both: the
   assistant can't recognise you without them.

## First run

**Models.** The first screen lists the models JARVIS needs and their size.
Press **Download**. Each file is checked against its SHA-256 before it is
used. If the connection drops, press **Resume**: what already arrived is kept.
If the disk is too full, it says how much space to free first.

**Language model.** Questions and free-form requests use a local model run by
[Ollama](https://ollama.com), a free app. The same screen detects it:

- Not installed: press **Get Ollama** (or run `brew install ollama` in
  Terminal), install it, then press **Check again**.
- Installed but not running: press **Start Ollama**.
- Running: press **Download** next to `qwen2.5:7b` (about 4.7 GB). `bge-m3`
  (memory recall) and `qwen2.5:3b` (Fast mode, used on battery) are optional.
  On a Mac with less than 12 GB of memory (an 8 GB MacBook Air, say), JARVIS asks
  for `qwen2.5:3b` (about 1.9 GB) instead, and keeps its models lighter: a smaller
  context, models released sooner, fewer threads, and no Whisper medium in Quality
  mode. Everything together then uses about 4.5 GB while answering and about 2 GB idle.

You can skip the language model and add it later in **Settings → On-device
models**. Until then, instant commands (timers, alarms, apps, notes, math, the
time) still work. Press **Continue** when the core models are ready.

**Setup.** Then comes setup:

1. Your name, and the assistant's name. Its name is also the wake word, so
   pick something you'll say easily (JARVIS, FRIDAY, EDITH…).
2. A female or male voice.
3. **Face scan:** follow the prompts (look straight, left, right, up, down,
   closer, farther, smile). Each pose is checked before it's recorded. Only
   encrypted numbers are kept, never photos.
4. **Voice:** read six short phrases in English, Hindi and Hinglish, in a quiet
   room.

## Talking to it

Say its name, then what you want, in English, हिंदी or Hinglish:

- "FRIDAY, set a timer for five minutes." / "FRIDAY, kal subah saat baje jagana."
- "FRIDAY, what's on my calendar tomorrow?"
- "FRIDAY, note to buy milk." / "FRIDAY, remember that my sister's birthday is 12 March."
- "FRIDAY, open Safari." / "WhatsApp band karo." / "open my documents"
- "FRIDAY, volume 30." / "next song" / "turn on dark mode" / "take a screenshot"
- "FRIDAY, the PDF I downloaded yesterday." / "show it in Finder" / "move it to the Trash"
- "FRIDAY, what's 18% of 2400?" / "how many days till Diwali?"
- "FRIDAY, open Claude and ask it to build a website for my bakery." It opens a
  new chat with the prompt written in, and waits for you to send it.

Say just the name and it answers with a short ping; then say the command. You
can also type commands in the command bar.

## Why it sometimes says no

The status bar shows the current **level**:

| Level | What it means | What works |
|---|---|---|
| L0 | It doesn't see you, or sees someone else, a photo or a frozen feed | nothing |
| L1 | It sees you and knows you're live | questions, reading your calendar, notes, files |
| L2 | L1 + it heard your voice in the last minute and nobody else is in view | creating things, opening apps, controlling the Mac |
| L3 | L2 + a recent liveness check + your "yes" | deleting anything |

If it says "talk to me first", say its name and anything: that verifies your
voice for a minute. If someone else is in view, it caps you at L1. For
deletions it shows exactly what will be deleted and waits 30 seconds for
"yes, go ahead" in your voice or a click on **Confirm**.

Now and then it asks for a quick liveness check (blink twice, turn your head,
lean in). That's what stops a photo of you from working.

## Keyboard

| Keys | Does |
|---|---|
| ⌘K | command palette: jump to any view or action |
| ⌘1 … ⌘7 | System, Authentication, Memory, Tools, Security, Privacy, Settings |
| / | focus the command bar |
| Esc | close the palette or the About view |
| ⌘I | About JARVIS |
| ⌘, | Settings |

## Privacy

- **Privacy view:** every kind of stored data with its size and age. Export a
  readable copy, delete any part, or factory-reset. Each needs level 3.
- **Offline mode** (on by default): the assistant's engine can't reach the
  internet. The status bar shows **OFFLINE ✓**.
- **Memory view:** facts it remembers and your conversation history (kept 30
  days by default; off, 7 days or forever).
- Camera frames and audio are never stored. Speech that doesn't start with its
  name is thrown away straight after transcription.

More in [SECURITY.md](SECURITY.md).

## Settings

Identity (names, re-scan face, re-record voice), voice speed and style,
microphone, security presets, performance mode (Fast / Balanced / Quality, or
Auto: Balanced on power, Fast on battery), language models, on-device models
(add Whisper medium for Quality mode), memory, the fusion model, file-search
folders and optional Apple Calendar / Notes sync.

## Troubleshooting

| Problem | Fix |
|---|---|
| "JARVIS can't be opened" | System Settings → Privacy & Security → Open Anyway (see Install) |
| "jarvis-backend wants to access key jarvis-assistant in your keychain" | Type your Mac login password and press **Always Allow**. JARVIS keeps its encryption key in the Keychain, and because the app is signed ad hoc, macOS asks once after each install or update |
| Camera unavailable | System Settings → Privacy & Security → Camera → turn on JARVIS; quit other apps using the camera; reopen JARVIS |
| Voice commands are off | Settings → Microphone says why and how to fix it (permission, no device, the wrong device, busy) |
| "Local AI offline" | Settings → On-device models → Start Ollama, or install it from ollama.com |
| A model is missing | Settings → On-device models → Download, then Restart |
| It doesn't recognise me in low light | Face the light; if it keeps happening, re-scan your face in Settings → Identity |
| A real face shows a low anti-spoof score | Better, even lighting helps; the score is in the Authentication panel |
| Screenshot / dark mode / typing don't work | macOS asks once for Screen Recording, Automation (System Events) and Accessibility; allow JARVIS in System Settings → Privacy & Security |
| File search finds nothing | Allow JARVIS for Desktop, Documents and Downloads in System Settings → Privacy & Security → Files and Folders |
| Locked out after deleting your face profile | Say its name in your verified voice to unlock a new face scan |
| The app shows "could not start" | Reinstall from the .dmg; if it persists, open an issue with the log from `~/Library/Application Support/JarvisAssistant/logs` |

## Uninstall

Quit JARVIS, then use **Privacy → Factory reset** (it also removes the Keychain
key), drag JARVIS from Applications to the Trash, and delete
`~/Library/Application Support/JarvisAssistant`. Ollama and its models are a
separate app: remove them with Ollama itself if you don't use them elsewhere.
